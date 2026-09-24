from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("ERP_DB_PATH", os.path.join(BASE_DIR, "erp.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SECRET_KEY = os.environ.get("ERP_SECRET_KEY") or os.environ.get("SESSION_SECRET") or "change-this-secret-in-production"
HTTPS_ONLY = os.environ.get("ERP_HTTPS_ONLY", "0") == "1"

app = FastAPI(title="Trade ERP", version="0.2.0")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, https_only=HTTPS_ONLY, same_site="lax")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

if DATABASE_URL:
    # Replit/Render can provide a plain PostgreSQL URL. Use psycopg v3 explicitly.
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
    if DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
else:
    # Local fallback remains available for testing on a PC.
    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(30), default="director")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    kind: Mapped[str] = mapped_column(String(30), default="client")  # client/supplier/both
    inn: Mapped[str] = mapped_column(String(20), default="")
    country: Mapped[str] = mapped_column(String(80), default="")
    contact: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(150), default="")
    phone: Mapped[str] = mapped_column(String(80), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(240))
    brand: Mapped[str] = mapped_column(String(100), default="")
    unit: Mapped[str] = mapped_column(String(20), default="шт")
    category: Mapped[str] = mapped_column(String(100), default="")
    spec: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RFQ(Base):
    __tablename__ = "rfqs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    title: Mapped[str] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(40), default="Новая")
    currency: Mapped[str] = mapped_column(String(10), default="RUB")
    deadline: Mapped[str] = mapped_column(String(30), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    client: Mapped[Company] = relationship()
    items: Mapped[list[RFQItem]] = relationship(back_populates="rfq", cascade="all, delete-orphan")


class RFQItem(Base):
    __tablename__ = "rfq_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rfq_id: Mapped[int] = mapped_column(ForeignKey("rfqs.id"))
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"), nullable=True)
    description: Mapped[str] = mapped_column(String(300))
    qty: Mapped[float] = mapped_column(Float, default=1)
    purchase_price: Mapped[float] = mapped_column(Float, default=0)
    purchase_currency: Mapped[str] = mapped_column(String(10), default="RMB")
    fx_rate: Mapped[float] = mapped_column(Float, default=1)
    logistics: Mapped[float] = mapped_column(Float, default=0)
    duty: Mapped[float] = mapped_column(Float, default=0)
    other_costs: Mapped[float] = mapped_column(Float, default=0)
    sale_price: Mapped[float] = mapped_column(Float, default=0)
    rfq: Mapped[RFQ] = relationship(back_populates="items")
    product: Mapped[Optional[Product]] = relationship()

    @property
    def landed_cost(self) -> float:
        return round(self.purchase_price * self.fx_rate + self.logistics + self.duty + self.other_costs, 2)

    @property
    def margin_amount(self) -> float:
        return round(self.sale_price - self.landed_cost, 2)

    @property
    def margin_percent(self) -> float:
        if not self.sale_price:
            return 0
        return round(self.margin_amount / self.sale_price * 100, 1)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    order_type: Mapped[str] = mapped_column(String(30), default="Продажа")
    status: Mapped[str] = mapped_column(String(40), default="Новый")
    amount: Mapped[float] = mapped_column(Float, default=0)
    currency: Mapped[str] = mapped_column(String(10), default="RUB")
    payment_status: Mapped[str] = mapped_column(String(40), default="Не оплачен")
    expected_date: Mapped[str] = mapped_column(String(30), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    company: Mapped[Company] = relationship()


Base.metadata.create_all(engine)


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        computed = hash_password(password, bytes.fromhex(salt_hex)).split("$", 1)[1]
        return hmac.compare_digest(computed, hash_hex)
    except Exception:
        return False


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401)
    user = db.get(User, user_id)
    if not user:
        request.session.clear()
        raise HTTPException(status_code=401)
    return user


def ensure_seed(db: Session):
    if not db.scalar(select(func.count()).select_from(User)):
        admin_password = os.environ.get("ERP_ADMIN_PASSWORD", "admin123")
        db.add(User(username="admin", password_hash=hash_password(admin_password), role="director"))
        db.commit()


@app.exception_handler(401)
async def unauthorized_handler(request: Request, exc):
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    ensure_seed(db)
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    ensure_seed(db)
    user = db.scalar(select(User).where(User.username == username.strip()))
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(request=request, name="login.html", context={"error": "Неверный логин или пароль"}, status_code=400)
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def ctx(request: Request, user: User, **kwargs):
    return {"request": request, "user": user, **kwargs}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    clients = db.scalar(select(func.count()).select_from(Company).where(Company.kind.in_(["client", "both"]))) or 0
    suppliers = db.scalar(select(func.count()).select_from(Company).where(Company.kind.in_(["supplier", "both"]))) or 0
    products = db.scalar(select(func.count()).select_from(Product)) or 0
    active_rfqs = db.scalar(select(func.count()).select_from(RFQ).where(RFQ.status.notin_(["Закрыта", "Отменена"]))) or 0
    sales = db.scalar(select(func.coalesce(func.sum(Order.amount), 0)).where(Order.order_type == "Продажа")) or 0
    recent_rfqs = db.scalars(select(RFQ).order_by(RFQ.created_at.desc()).limit(8)).all()
    recent_orders = db.scalars(select(Order).order_by(Order.created_at.desc()).limit(8)).all()
    return templates.TemplateResponse(request=request, name="dashboard.html", context=ctx(request, user, stats={
        "clients": clients, "suppliers": suppliers, "products": products, "active_rfqs": active_rfqs, "sales": sales
    }, recent_rfqs=recent_rfqs, recent_orders=recent_orders))


@app.get("/companies", response_class=HTMLResponse)
def companies(request: Request, kind: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    stmt = select(Company).order_by(Company.name)
    if kind:
        stmt = stmt.where(Company.kind == kind)
    rows = db.scalars(stmt).all()
    return templates.TemplateResponse(request=request, name="companies.html", context=ctx(request, user, companies=rows, kind=kind))


@app.post("/companies")
def add_company(
    name: str = Form(...), kind: str = Form(...), inn: str = Form(""), country: str = Form(""),
    contact: str = Form(""), email: str = Form(""), phone: str = Form(""), notes: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(current_user)
):
    db.add(Company(name=name.strip(), kind=kind, inn=inn.strip(), country=country.strip(), contact=contact.strip(), email=email.strip(), phone=phone.strip(), notes=notes.strip()))
    db.commit()
    return RedirectResponse("/companies", status_code=303)


@app.post("/companies/{company_id}/delete")
def delete_company(company_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    company = db.get(Company, company_id)
    if company:
        try:
            db.delete(company); db.commit()
        except Exception:
            db.rollback()
    return RedirectResponse("/companies", status_code=303)


@app.get("/products", response_class=HTMLResponse)
def products(request: Request, q: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    stmt = select(Product).order_by(Product.created_at.desc())
    if q:
        like = f"%{q}%"
        stmt = stmt.where((Product.name.ilike(like)) | (Product.sku.ilike(like)) | (Product.brand.ilike(like)))
    rows = db.scalars(stmt).all()
    return templates.TemplateResponse(request=request, name="products.html", context=ctx(request, user, products=rows, q=q))


@app.post("/products")
def add_product(
    sku: str = Form(...), name: str = Form(...), brand: str = Form(""), unit: str = Form("шт"), category: str = Form(""), spec: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(current_user)
):
    if db.scalar(select(Product).where(Product.sku == sku.strip())):
        return RedirectResponse("/products?error=duplicate", status_code=303)
    db.add(Product(sku=sku.strip(), name=name.strip(), brand=brand.strip(), unit=unit.strip(), category=category.strip(), spec=spec.strip()))
    db.commit()
    return RedirectResponse("/products", status_code=303)


@app.get("/rfqs", response_class=HTMLResponse)
def rfqs(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(RFQ).order_by(RFQ.created_at.desc())).all()
    clients = db.scalars(select(Company).where(Company.kind.in_(["client", "both"])).order_by(Company.name)).all()
    return templates.TemplateResponse(request=request, name="rfqs.html", context=ctx(request, user, rfqs=rows, clients=clients))


@app.post("/rfqs")
def add_rfq(
    client_id: int = Form(...), title: str = Form(...), deadline: str = Form(""), notes: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(current_user)
):
    year = datetime.now().year
    count = (db.scalar(select(func.count()).select_from(RFQ)) or 0) + 1
    number = f"RFQ-{year}-{count:04d}"
    db.add(RFQ(number=number, client_id=client_id, title=title.strip(), deadline=deadline.strip(), notes=notes.strip()))
    db.commit()
    return RedirectResponse("/rfqs", status_code=303)


@app.get("/rfqs/{rfq_id}", response_class=HTMLResponse)
def rfq_detail(rfq_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rfq = db.get(RFQ, rfq_id)
    if not rfq:
        raise HTTPException(404)
    products = db.scalars(select(Product).order_by(Product.name)).all()
    total_cost = sum(i.landed_cost * i.qty for i in rfq.items)
    total_sale = sum(i.sale_price * i.qty for i in rfq.items)
    profit = total_sale - total_cost
    margin = (profit / total_sale * 100) if total_sale else 0
    return templates.TemplateResponse(request=request, name="rfq_detail.html", context=ctx(request, user, rfq=rfq, products=products, totals={
        "cost": total_cost, "sale": total_sale, "profit": profit, "margin": margin
    }))


@app.post("/rfqs/{rfq_id}/status")
def update_rfq_status(rfq_id: int, status: str = Form(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    rfq = db.get(RFQ, rfq_id)
    if not rfq:
        raise HTTPException(404)
    rfq.status = status
    db.commit()
    return RedirectResponse(f"/rfqs/{rfq_id}", status_code=303)


@app.post("/rfqs/{rfq_id}/items")
def add_rfq_item(
    rfq_id: int, product_id: str = Form(""), description: str = Form(...), qty: float = Form(1),
    purchase_price: float = Form(0), purchase_currency: str = Form("RMB"), fx_rate: float = Form(1),
    logistics: float = Form(0), duty: float = Form(0), other_costs: float = Form(0), sale_price: float = Form(0),
    db: Session = Depends(get_db), user: User = Depends(current_user)
):
    if not db.get(RFQ, rfq_id):
        raise HTTPException(404)
    pid = int(product_id) if product_id.strip() else None
    db.add(RFQItem(rfq_id=rfq_id, product_id=pid, description=description.strip(), qty=qty, purchase_price=purchase_price,
                   purchase_currency=purchase_currency, fx_rate=fx_rate, logistics=logistics, duty=duty,
                   other_costs=other_costs, sale_price=sale_price))
    db.commit()
    return RedirectResponse(f"/rfqs/{rfq_id}", status_code=303)


@app.post("/rfqs/{rfq_id}/items/{item_id}/delete")
def delete_rfq_item(rfq_id: int, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = db.get(RFQItem, item_id)
    if item and item.rfq_id == rfq_id:
        db.delete(item); db.commit()
    return RedirectResponse(f"/rfqs/{rfq_id}", status_code=303)


@app.get("/orders", response_class=HTMLResponse)
def orders(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(Order).order_by(Order.created_at.desc())).all()
    companies = db.scalars(select(Company).order_by(Company.name)).all()
    return templates.TemplateResponse(request=request, name="orders.html", context=ctx(request, user, orders=rows, companies=companies))


@app.post("/orders")
def add_order(
    company_id: int = Form(...), order_type: str = Form(...), amount: float = Form(0), currency: str = Form("RUB"),
    expected_date: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)
):
    prefix = "SO" if order_type == "Продажа" else "PO"
    count = (db.scalar(select(func.count()).select_from(Order)) or 0) + 1
    number = f"{prefix}-{datetime.now().year}-{count:04d}"
    db.add(Order(number=number, company_id=company_id, order_type=order_type, amount=amount, currency=currency,
                 expected_date=expected_date.strip(), notes=notes.strip()))
    db.commit()
    return RedirectResponse("/orders", status_code=303)


@app.post("/orders/{order_id}/update")
def update_order(order_id: int, status: str = Form(...), payment_status: str = Form(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404)
    order.status = status
    order.payment_status = payment_status
    db.commit()
    return RedirectResponse("/orders", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok", "app": "trade-erp", "database": "postgresql" if DATABASE_URL else "sqlite"
