require('dotenv').config();
const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const jwt = require('jsonwebtoken');
const bcrypt = require('bcryptjs');
const { Pool } = require('pg');
const multer = require('multer');
const cors = require('cors');
const { v4: uuidv4 } = require('uuid');
const path = require('path');
const fs = require('fs');
const fetch = require('node-fetch');

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

// ─── Config ────────────────────────────────────────────────────────────────
const PORT = process.env.PORT || 3000;
const JWT_SECRET = process.env.JWT_SECRET || 'nexus-super-secret-key-change-in-prod';
const DB_URL = process.env.DATABASE_URL;

// ─── Database ───────────────────────────────────────────────────────────────
const pool = new Pool({
  connectionString: DB_URL,
  ssl: DB_URL && DB_URL.includes('railway') ? { rejectUnauthorized: false } : false
});

async function initDB() {
  const client = await pool.connect();
  try {
    await client.query(`
      CREATE TABLE IF NOT EXISTS users (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        username VARCHAR(50) UNIQUE NOT NULL,
        email VARCHAR(100) UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        avatar TEXT DEFAULT NULL,
        is_admin BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMPTZ DEFAULT NOW()
      );

      CREATE TABLE IF NOT EXISTS chats (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name VARCHAR(100),
        type VARCHAR(20) NOT NULL CHECK (type IN ('private','group','channel')),
        description TEXT,
        avatar TEXT,
        owner_id UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
      );

      CREATE TABLE IF NOT EXISTS chat_members (
        chat_id UUID REFERENCES chats(id) ON DELETE CASCADE,
        user_id UUID REFERENCES users(id) ON DELETE CASCADE,
        role VARCHAR(20) DEFAULT 'member',
        joined_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (chat_id, user_id)
      );

      CREATE TABLE IF NOT EXISTS messages (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        chat_id UUID REFERENCES chats(id) ON DELETE CASCADE,
        user_id UUID REFERENCES users(id) ON DELETE SET NULL,
        content TEXT,
        type VARCHAR(20) DEFAULT 'text',
        file_url TEXT,
        file_name TEXT,
        reply_to UUID REFERENCES messages(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
      );

      CREATE TABLE IF NOT EXISTS bots (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name VARCHAR(100) NOT NULL,
        username VARCHAR(50) UNIQUE NOT NULL,
        description TEXT,
        provider VARCHAR(50) DEFAULT 'groq',
        api_key TEXT,
        model VARCHAR(100) DEFAULT 'llama3-8b-8192',
        system_prompt TEXT DEFAULT 'You are a helpful AI assistant in a chat app. Be friendly and concise.',
        is_active BOOLEAN DEFAULT TRUE,
        created_by UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
      );

      CREATE TABLE IF NOT EXISTS bot_chats (
        bot_id UUID REFERENCES bots(id) ON DELETE CASCADE,
        chat_id UUID REFERENCES chats(id) ON DELETE CASCADE,
        PRIMARY KEY (bot_id, chat_id)
      );

      CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, created_at DESC);
      CREATE INDEX IF NOT EXISTS idx_chat_members_user ON chat_members(user_id);
    `);
    console.log('✅ Database initialized');
  } finally {
    client.release();
  }
}

// ─── Middleware ──────────────────────────────────────────────────────────────
app.use(cors({ origin: '*', credentials: true }));
app.use(express.json({ limit: '50mb' }));
app.use(express.static(path.join(__dirname, '../public')));

const uploadsDir = path.join(__dirname, '../uploads');
if (!fs.existsSync(uploadsDir)) fs.mkdirSync(uploadsDir, { recursive: true });
app.use('/uploads', express.static(uploadsDir));

const upload = multer({
  dest: uploadsDir,
  limits: { fileSize: 50 * 1024 * 1024 },
  fileFilter: (req, file, cb) => {
    const allowed = /jpeg|jpg|png|gif|webp|mp4|mp3|ogg|pdf|doc|docx|txt|zip/;
    cb(null, allowed.test(path.extname(file.originalname).toLowerCase()));
  }
});

function auth(req, res, next) {
  const token = req.headers.authorization?.split(' ')[1];
  if (!token) return res.status(401).json({ error: 'No token' });
  try {
    req.user = jwt.verify(token, JWT_SECRET);
    next();
  } catch {
    res.status(401).json({ error: 'Invalid token' });
  }
}

function adminAuth(req, res, next) {
  auth(req, res, async () => {
    const r = await pool.query('SELECT is_admin FROM users WHERE id=$1', [req.user.id]);
    if (!r.rows[0]?.is_admin) return res.status(403).json({ error: 'Admin only' });
    next();
  });
}

// ─── Auth Routes ─────────────────────────────────────────────────────────────
app.post('/api/auth/register', async (req, res) => {
  const { username, email, password } = req.body;
  if (!username || !email || !password)
    return res.status(400).json({ error: 'All fields required' });
  if (password.length < 6)
    return res.status(400).json({ error: 'Password must be at least 6 characters' });
  try {
    const hash = await bcrypt.hash(password, 10);
    // First user becomes admin
    const countR = await pool.query('SELECT COUNT(*) FROM users');
    const isAdmin = parseInt(countR.rows[0].count) === 0;
    const r = await pool.query(
      'INSERT INTO users(username,email,password_hash,is_admin) VALUES($1,$2,$3,$4) RETURNING id,username,email,is_admin,created_at',
      [username, email, hash, isAdmin]
    );
    const user = r.rows[0];
    const token = jwt.sign({ id: user.id, username: user.username }, JWT_SECRET, { expiresIn: '30d' });
    res.json({ token, user });
  } catch (e) {
    if (e.code === '23505') return res.status(400).json({ error: 'Username or email already exists' });
    console.error(e);
    res.status(500).json({ error: 'Server error' });
  }
});

app.post('/api/auth/login', async (req, res) => {
  const { login, password } = req.body;
  try {
    const r = await pool.query(
      'SELECT * FROM users WHERE username=$1 OR email=$1', [login]
    );
    const user = r.rows[0];
    if (!user || !(await bcrypt.compare(password, user.password_hash)))
      return res.status(400).json({ error: 'Invalid credentials' });
    const token = jwt.sign({ id: user.id, username: user.username }, JWT_SECRET, { expiresIn: '30d' });
    const { password_hash, ...safeUser } = user;
    res.json({ token, user: safeUser });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: 'Server error' });
  }
});

app.get('/api/auth/me', auth, async (req, res) => {
  const r = await pool.query('SELECT id,username,email,avatar,is_admin,created_at FROM users WHERE id=$1', [req.user.id]);
  res.json(r.rows[0]);
});

// ─── User Routes ──────────────────────────────────────────────────────────────
app.get('/api/users/search', auth, async (req, res) => {
  const { q } = req.query;
  const r = await pool.query(
    'SELECT id,username,email,avatar FROM users WHERE username ILIKE $1 AND id != $2 LIMIT 20',
    [`%${q}%`, req.user.id]
  );
  res.json(r.rows);
});

app.put('/api/users/me', auth, async (req, res) => {
  const { username, avatar } = req.body;
  const r = await pool.query(
    'UPDATE users SET username=COALESCE($1,username), avatar=COALESCE($2,avatar) WHERE id=$3 RETURNING id,username,email,avatar,is_admin',
    [username, avatar, req.user.id]
  );
  res.json(r.rows[0]);
});

// ─── Chat Routes ──────────────────────────────────────────────────────────────
app.get('/api/chats', auth, async (req, res) => {
  const r = await pool.query(`
    SELECT c.*, 
      (SELECT content FROM messages WHERE chat_id=c.id ORDER BY created_at DESC LIMIT 1) as last_message,
      (SELECT created_at FROM messages WHERE chat_id=c.id ORDER BY created_at DESC LIMIT 1) as last_message_at,
      (SELECT COUNT(*) FROM chat_members WHERE chat_id=c.id) as member_count
    FROM chats c
    JOIN chat_members cm ON cm.chat_id=c.id
    WHERE cm.user_id=$1
    ORDER BY last_message_at DESC NULLS LAST, c.created_at DESC
  `, [req.user.id]);
  res.json(r.rows);
});

app.post('/api/chats', auth, async (req, res) => {
  const { name, type, description, member_ids } = req.body;
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const chatR = await client.query(
      'INSERT INTO chats(name,type,description,owner_id) VALUES($1,$2,$3,$4) RETURNING *',
      [name, type || 'group', description, req.user.id]
    );
    const chat = chatR.rows[0];
    await client.query('INSERT INTO chat_members(chat_id,user_id,role) VALUES($1,$2,$3)',
      [chat.id, req.user.id, 'admin']);
    if (member_ids?.length) {
      for (const uid of member_ids) {
        await client.query('INSERT INTO chat_members(chat_id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING', [chat.id, uid]);
      }
    }
    await client.query('COMMIT');
    res.json(chat);
  } catch (e) {
    await client.query('ROLLBACK');
    console.error(e);
    res.status(500).json({ error: 'Server error' });
  } finally {
    client.release();
  }
});

app.post('/api/chats/private', auth, async (req, res) => {
  const { user_id } = req.body;
  // Check if private chat already exists
  const existing = await pool.query(`
    SELECT c.* FROM chats c
    JOIN chat_members cm1 ON cm1.chat_id=c.id AND cm1.user_id=$1
    JOIN chat_members cm2 ON cm2.chat_id=c.id AND cm2.user_id=$2
    WHERE c.type='private'
    LIMIT 1
  `, [req.user.id, user_id]);
  if (existing.rows.length) return res.json(existing.rows[0]);

  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const chatR = await client.query(
      "INSERT INTO chats(type,owner_id) VALUES('private',$1) RETURNING *", [req.user.id]
    );
    const chat = chatR.rows[0];
    await client.query('INSERT INTO chat_members(chat_id,user_id) VALUES($1,$2),($1,$3)', [chat.id, req.user.id, user_id]);
    await client.query('COMMIT');
    res.json(chat);
  } catch (e) {
    await client.query('ROLLBACK');
    res.status(500).json({ error: 'Server error' });
  } finally {
    client.release();
  }
});

app.get('/api/chats/:id/messages', auth, async (req, res) => {
  const { id } = req.params;
  const { before, limit = 50 } = req.query;
  // Verify membership
  const mem = await pool.query('SELECT 1 FROM chat_members WHERE chat_id=$1 AND user_id=$2', [id, req.user.id]);
  if (!mem.rows.length) return res.status(403).json({ error: 'Not a member' });

  let query = `
    SELECT m.*, u.username, u.avatar,
      rm.content as reply_content, ru.username as reply_username
    FROM messages m
    LEFT JOIN users u ON u.id=m.user_id
    LEFT JOIN messages rm ON rm.id=m.reply_to
    LEFT JOIN users ru ON ru.id=rm.user_id
    WHERE m.chat_id=$1
  `;
  const params = [id];
  if (before) {
    params.push(before);
    query += ` AND m.created_at < $${params.length}`;
  }
  query += ` ORDER BY m.created_at DESC LIMIT $${params.length + 1}`;
  params.push(parseInt(limit));

  const r = await pool.query(query, params);
  res.json(r.rows.reverse());
});

app.get('/api/chats/:id/members', auth, async (req, res) => {
  const r = await pool.query(`
    SELECT u.id, u.username, u.avatar, cm.role, cm.joined_at
    FROM chat_members cm JOIN users u ON u.id=cm.user_id
    WHERE cm.chat_id=$1
  `, [req.params.id]);
  res.json(r.rows);
});

app.post('/api/chats/:id/members', auth, async (req, res) => {
  const { user_id } = req.body;
  await pool.query('INSERT INTO chat_members(chat_id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING', [req.params.id, user_id]);
  res.json({ ok: true });
});

app.delete('/api/chats/:id/members/:userId', auth, async (req, res) => {
  await pool.query('DELETE FROM chat_members WHERE chat_id=$1 AND user_id=$2', [req.params.id, req.params.userId]);
  res.json({ ok: true });
});

// ─── Message Routes ───────────────────────────────────────────────────────────
app.post('/api/messages', auth, async (req, res) => {
  const { chat_id, content, type, reply_to } = req.body;
  const mem = await pool.query('SELECT 1 FROM chat_members WHERE chat_id=$1 AND user_id=$2', [chat_id, req.user.id]);
  if (!mem.rows.length) return res.status(403).json({ error: 'Not a member' });

  const r = await pool.query(
    'INSERT INTO messages(chat_id,user_id,content,type,reply_to) VALUES($1,$2,$3,$4,$5) RETURNING *',
    [chat_id, req.user.id, content, type || 'text', reply_to || null]
  );
  const msg = r.rows[0];
  const userR = await pool.query('SELECT username, avatar FROM users WHERE id=$1', [req.user.id]);
  const fullMsg = { ...msg, ...userR.rows[0] };

  // Broadcast via WS
  broadcastToChat(chat_id, { type: 'message', data: fullMsg });
  res.json(fullMsg);

  // Trigger AI bot response
  triggerBotResponse(chat_id, content, req.user.username);
});

app.post('/api/messages/file', auth, upload.single('file'), async (req, res) => {
  const { chat_id, reply_to } = req.body;
  if (!req.file) return res.status(400).json({ error: 'No file' });
  const fileUrl = `/uploads/${req.file.filename}`;
  const r = await pool.query(
    'INSERT INTO messages(chat_id,user_id,content,type,file_url,file_name,reply_to) VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *',
    [chat_id, req.user.id, req.file.originalname, 'file', fileUrl, req.file.originalname, reply_to || null]
  );
  const msg = r.rows[0];
  const userR = await pool.query('SELECT username, avatar FROM users WHERE id=$1', [req.user.id]);
  const fullMsg = { ...msg, ...userR.rows[0] };
  broadcastToChat(chat_id, { type: 'message', data: fullMsg });
  res.json(fullMsg);
});

// ─── Bot Routes ────────────────────────────────────────────────────────────────
app.get('/api/bots', auth, async (req, res) => {
  const r = await pool.query('SELECT id,name,username,description,provider,model,system_prompt,is_active,created_at FROM bots ORDER BY created_at');
  res.json(r.rows);
});

app.post('/api/bots', auth, async (req, res) => {
  const { name, username, description, provider, api_key, model, system_prompt } = req.body;
  if (!name || !username) return res.status(400).json({ error: 'Name and username required' });
  try {
    const r = await pool.query(
      'INSERT INTO bots(name,username,description,provider,api_key,model,system_prompt,created_by) VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id,name,username,description,provider,model,system_prompt,is_active',
      [name, username, description, provider || 'groq', api_key, model || 'llama3-8b-8192', system_prompt || 'You are a helpful AI assistant. Be friendly, creative and concise.', req.user.id]
    );
    res.json(r.rows[0]);
  } catch (e) {
    if (e.code === '23505') return res.status(400).json({ error: 'Bot username already exists' });
    res.status(500).json({ error: 'Server error' });
  }
});

app.put('/api/bots/:id', auth, async (req, res) => {
  const { name, description, api_key, model, system_prompt, is_active } = req.body;
  const r = await pool.query(
    'UPDATE bots SET name=COALESCE($1,name),description=COALESCE($2,description),api_key=COALESCE($3,api_key),model=COALESCE($4,model),system_prompt=COALESCE($5,system_prompt),is_active=COALESCE($6,is_active) WHERE id=$7 RETURNING id,name,username,description,provider,model,system_prompt,is_active',
    [name, description, api_key, model, system_prompt, is_active, req.params.id]
  );
  res.json(r.rows[0]);
});

app.delete('/api/bots/:id', auth, async (req, res) => {
  await pool.query('DELETE FROM bots WHERE id=$1', [req.params.id]);
  res.json({ ok: true });
});

app.post('/api/bots/:id/add-to-chat', auth, async (req, res) => {
  const { chat_id } = req.body;
  await pool.query('INSERT INTO bot_chats(bot_id,chat_id) VALUES($1,$2) ON CONFLICT DO NOTHING', [req.params.id, chat_id]);
  res.json({ ok: true });
});

app.delete('/api/bots/:id/remove-from-chat', auth, async (req, res) => {
  const { chat_id } = req.body;
  await pool.query('DELETE FROM bot_chats WHERE bot_id=$1 AND chat_id=$2', [req.params.id, chat_id]);
  res.json({ ok: true });
});

app.get('/api/chats/:id/bots', auth, async (req, res) => {
  const r = await pool.query(`
    SELECT b.id, b.name, b.username, b.description, b.is_active
    FROM bots b JOIN bot_chats bc ON bc.bot_id=b.id
    WHERE bc.chat_id=$1
  `, [req.params.id]);
  res.json(r.rows);
});

// ─── Admin Routes ─────────────────────────────────────────────────────────────
app.get('/api/admin/stats', adminAuth, async (req, res) => {
  const [users, chats, messages, bots] = await Promise.all([
    pool.query('SELECT COUNT(*) FROM users'),
    pool.query('SELECT COUNT(*) FROM chats'),
    pool.query('SELECT COUNT(*) FROM messages'),
    pool.query('SELECT COUNT(*) FROM bots')
  ]);
  res.json({
    users: parseInt(users.rows[0].count),
    chats: parseInt(chats.rows[0].count),
    messages: parseInt(messages.rows[0].count),
    bots: parseInt(bots.rows[0].count)
  });
});

app.get('/api/admin/users', adminAuth, async (req, res) => {
  const r = await pool.query('SELECT id,username,email,is_admin,created_at FROM users ORDER BY created_at DESC');
  res.json(r.rows);
});

app.put('/api/admin/users/:id', adminAuth, async (req, res) => {
  const { is_admin } = req.body;
  const r = await pool.query('UPDATE users SET is_admin=$1 WHERE id=$2 RETURNING id,username,email,is_admin', [is_admin, req.params.id]);
  res.json(r.rows[0]);
});

app.delete('/api/admin/users/:id', adminAuth, async (req, res) => {
  await pool.query('DELETE FROM users WHERE id=$1', [req.params.id]);
  res.json({ ok: true });
});

// ─── AI Bot Engine ─────────────────────────────────────────────────────────────
async function triggerBotResponse(chatId, userMessage, username) {
  try {
    const botsR = await pool.query(`
      SELECT b.* FROM bots b
      JOIN bot_chats bc ON bc.bot_id=b.id
      WHERE bc.chat_id=$1 AND b.is_active=TRUE
    `, [chatId]);

    for (const bot of botsR.rows) {
      if (!bot.api_key) continue;
      // Check if message mentions bot or starts with @botname
      const mentioned = userMessage.toLowerCase().includes(`@${bot.username.toLowerCase()}`) ||
                        userMessage.toLowerCase().includes(bot.name.toLowerCase());
      if (!mentioned && botsR.rows.length > 1) continue; // In multi-bot chats, require mention

      setTimeout(() => sendBotMessage(bot, chatId, userMessage, username), 500);
    }
  } catch (e) {
    console.error('Bot trigger error:', e.message);
  }
}

async function sendBotMessage(bot, chatId, userMessage, username) {
  try {
    let responseText = '';

    if (bot.provider === 'groq') {
      const resp = await fetch('https://api.groq.com/openai/v1/chat/completions', {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${bot.api_key}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: bot.model || 'llama3-8b-8192',
          messages: [
            { role: 'system', content: bot.system_prompt },
            { role: 'user', content: `[${username}]: ${userMessage}` }
          ],
          max_tokens: 500,
          temperature: 0.8
        })
      });
      const data = await resp.json();
      responseText = data.choices?.[0]?.message?.content || 'Hmm, I cannot respond right now.';
    } else if (bot.provider === 'openai') {
      const resp = await fetch('https://api.openai.com/v1/chat/completions', {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${bot.api_key}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: bot.model || 'gpt-3.5-turbo',
          messages: [
            { role: 'system', content: bot.system_prompt },
            { role: 'user', content: `[${username}]: ${userMessage}` }
          ],
          max_tokens: 500
        })
      });
      const data = await resp.json();
      responseText = data.choices?.[0]?.message?.content || 'Cannot respond now.';
    } else if (bot.provider === 'anthropic') {
      const resp = await fetch('https://api.anthropic.com/v1/messages', {
        method: 'POST',
        headers: {
          'x-api-key': bot.api_key,
          'anthropic-version': '2023-06-01',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          model: bot.model || 'claude-haiku-4-5-20251001',
          max_tokens: 500,
          system: bot.system_prompt,
          messages: [{ role: 'user', content: `[${username}]: ${userMessage}` }]
        })
      });
      const data = await resp.json();
      responseText = data.content?.[0]?.text || 'Cannot respond now.';
    }

    if (!responseText) return;

    // Find or create a virtual user for the bot
    let botUserR = await pool.query('SELECT id FROM users WHERE username=$1', [bot.username]);
    let botUserId;
    if (!botUserR.rows.length) {
      const hash = await bcrypt.hash(uuidv4(), 10);
      const newUser = await pool.query(
        'INSERT INTO users(username,email,password_hash) VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING id',
        [bot.username, `${bot.username}@bot.nexus`, hash]
      );
      if (newUser.rows.length) {
        botUserId = newUser.rows[0].id;
        await pool.query('INSERT INTO chat_members(chat_id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING', [chatId, botUserId]);
      } else {
        botUserR = await pool.query('SELECT id FROM users WHERE username=$1', [bot.username]);
        botUserId = botUserR.rows[0].id;
      }
    } else {
      botUserId = botUserR.rows[0].id;
    }

    const msgR = await pool.query(
      'INSERT INTO messages(chat_id,user_id,content,type) VALUES($1,$2,$3,$4) RETURNING *',
      [chatId, botUserId, responseText, 'bot']
    );
    const msg = { ...msgR.rows[0], username: bot.name, avatar: null, is_bot: true };
    broadcastToChat(chatId, { type: 'message', data: msg });
  } catch (e) {
    console.error('Bot response error:', e.message);
  }
}

// ─── WebSocket ────────────────────────────────────────────────────────────────
const clients = new Map(); // userId -> Set of ws

wss.on('connection', (ws, req) => {
  const token = new URL(req.url, 'ws://x').searchParams.get('token');
  let userId = null;

  if (token) {
    try {
      const decoded = jwt.verify(token, JWT_SECRET);
      userId = decoded.id;
      if (!clients.has(userId)) clients.set(userId, new Set());
      clients.get(userId).add(ws);
    } catch {}
  }

  ws.on('message', (raw) => {
    try {
      const msg = JSON.parse(raw);
      if (msg.type === 'typing') {
        broadcastToChat(msg.chat_id, {
          type: 'typing',
          data: { user_id: userId, username: msg.username, chat_id: msg.chat_id }
        }, userId);
      }
    } catch {}
  });

  ws.on('close', () => {
    if (userId && clients.has(userId)) {
      clients.get(userId).delete(ws);
      if (!clients.get(userId).size) clients.delete(userId);
    }
  });
});

async function broadcastToChat(chatId, payload, excludeUserId = null) {
  try {
    const membersR = await pool.query('SELECT user_id FROM chat_members WHERE chat_id=$1', [chatId]);
    const json = JSON.stringify(payload);
    for (const row of membersR.rows) {
      if (row.user_id === excludeUserId) continue;
      const userWs = clients.get(row.user_id);
      if (userWs) {
        for (const ws of userWs) {
          if (ws.readyState === WebSocket.OPEN) ws.send(json);
        }
      }
    }
  } catch (e) {
    console.error('Broadcast error:', e.message);
  }
}

// ─── SPA Fallback ─────────────────────────────────────────────────────────────
app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, '../public/index.html'));
});

// ─── Start ────────────────────────────────────────────────────────────────────
async function start() {
  if (!DB_URL) {
    console.error('❌ DATABASE_URL is not set. Please set it in your environment variables.');
    process.exit(1);
  }
  await initDB();
  server.listen(PORT, () => console.log(`🚀 NexusChat running on port ${PORT}`));
}

start().catch(e => { console.error('Startup failed:', e); process.exit(1); });
