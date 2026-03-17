@router.post("/register", response_model=schemas.UserOut)
async def register(user: schemas.UserCreate):
    try:
        db_user = User(**user.dict())
        await db_user.save()
        return schemas.UserOut.from_attributes(db_user)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/login", response_model=schemas.UserOut)
async def login(user: schemas.UserLogin):
    try:
        db_user = await User.get(user.username)
        if db_user and db_user.verify_password(user.password):
            return schemas.UserOut.from_attributes(db_user)
        raise HTTPException(status_code=400, detail="Invalid username or password")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))