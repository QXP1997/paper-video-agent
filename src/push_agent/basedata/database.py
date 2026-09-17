# database.py

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from push_agent.basedata.models import Base

DATABASE_URL = "sqlite:///D:/push_agent/src/push_agent/basedata/push_agent.db"


engine = create_engine(
    DATABASE_URL,
    echo=True,
)


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


def init_db():
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    init_db()