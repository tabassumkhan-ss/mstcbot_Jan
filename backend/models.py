import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    DateTime, ForeignKey, BigInteger, Boolean
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")


engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
)


SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


Base = declarative_base()


# ===================== MODELS =====================

class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)
    username = Column(String)
    first_name = Column(String)
    role = Column(String, default="user")
    self_activated = Column(Boolean, default=False)
    total_team_business = Column(Float, default=0.0)
    active_origin_count = Column(Integer, default=0)
    balance_musd = Column(Float, default=0.0)
    balance_mstc = Column(Float, default=0.0)
    referrer_id = Column(BigInteger, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    referrer = relationship("User", remote_side=[id])


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    amount = Column(Float)
    currency = Column(String)
    type = Column(String)
    external_id = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class ReferralEvent(Base):
    __tablename__ = "referral_events"

    id = Column(Integer, primary_key=True)
    from_user = Column(BigInteger)
    to_user = Column(BigInteger)
    amount = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)



Base.metadata.create_all(bind=engine)
