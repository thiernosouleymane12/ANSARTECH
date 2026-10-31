#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ANSARTECH 224 — Logiciel de gestion commerciale multi-entreprises (fichier unique)
==================================================================================
Produits, stocks, achats, ventes, factures, analyses, archives, messagerie,
plateforme multi-entreprises (abonnements, adresses, logo et facture par entreprise).

Tout le logiciel est dans ce seul fichier : code Python, pages HTML, styles et scripts.

En local :     pip install -r requirements.txt   puis   python app.py
               → http://127.0.0.1:5000   (admin / Admin@224)
Sur Render :   gunicorn app:app --preload --workers 2 --threads 2 --timeout 120 --bind 0.0.0.0:$PORT

Sommaire (cherchez ces titres) :
    • ÉTAPE 1 — CONFIGURATION
    • EXTENSIONS (base de données, connexion, CSRF)
    • ÉTAPE 2 — BASE DE DONNÉES (modèles)
    • MULTI-ENTREPRISES (isolation automatique des données)
    • FONCTIONS UTILITAIRES
    • INITIALISATION, CRÉATION D'ENTREPRISE, DÉMONSTRATION
    • PDF PROFESSIONNEL ET MONTANT EN LETTRES
    • ÉTAPE 3 — CONNEXION ET UTILISATEURS
    • ÉTAPE 4 — TABLEAU DE BORD
    • ÉTAPE 5 — PRODUITS
    • ÉTAPE 6 — STOCKS
    • ÉTAPE 7 — ACHATS ET FOURNISSEURS
    • ÉTAPE 8 — VENTES ET FACTURATION
    • ÉTAPE 9 — ANALYSES ET RECOMMANDATIONS
    • ÉTAPE 10 — ARCHIVES, DOCUMENTS ET MESSAGES
    • ÉTAPE 11 — PARAMÈTRES ET SÉCURITÉ
    • ESPACE PLATEFORME (super-admin, entreprises clientes)
    • FACTURE PRO (client libre, lignes, PDF, WhatsApp)
    • CRÉATION DE L'APPLICATION
    • PAGES HTML (templates)
    • STYLES, SCRIPTS ET IMAGES (fichiers statiques)
    • DÉMARRAGE
"""

import csv
import io
import json
import math
import mimetypes
import os
import random
import re
import secrets
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from urllib.parse import quote, urlparse
from xml.sax.saxutils import escape

import click
from dotenv import load_dotenv
from flask import (Blueprint, Flask, Response, abort, current_app, flash, g, has_request_context,
    redirect, render_template, request, send_file, session, url_for)
from flask_login import (LoginManager, UserMixin, current_user, login_required, login_user,
    logout_user)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
    TableStyle)
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session, with_loader_criteria
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from jinja2 import DictLoader


# ==================================================================================================
# ÉTAPE 1 — CONFIGURATION
# ==================================================================================================

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return "sqlite:///" + os.path.join(BASE_DIR, "ansartech224.db")
    # Render fournit parfois "postgres://" : SQLAlchemy exige "postgresql://"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    APP_NAME = "ANSARTECH 224"
    SECRET_KEY = os.environ.get("SECRET_KEY") or "dev-cle-a-changer-en-production"
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 280}

    # Sécurité des sessions
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("FLASK_ENV") == "production"
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    WTF_CSRF_TIME_LIMIT = None

    # Documents (stockés en base : le disque de Render est effacé à chaque déploiement)
    MAX_CONTENT_LENGTH = 6 * 1024 * 1024  # 6 Mo par requête
    MAX_DOCUMENT_SIZE = 5 * 1024 * 1024  # 5 Mo par fichier
    ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "gif", "webp", "doc", "docx",
                          "xls", "xlsx", "csv", "txt", "odt", "ods"}

    # Super-administrateur de la plateforme (vous), créé au premier démarrage
    ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME") or "admin"
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or "Admin@224"
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL") or "admin@ansartech224.com"

    # Verrouillage après échecs de connexion
    MAX_LOGIN_ATTEMPTS = 5
    LOCKOUT_MINUTES = 15


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = False
    SESSION_COOKIE_SECURE = False
    ADMIN_USERNAME = "super"
    ADMIN_PASSWORD = "Super@224"


# ==================================================================================================
# EXTENSIONS (base de données, connexion, CSRF)
# ==================================================================================================

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()

login_manager.login_view = "auth.login"
login_manager.login_message = "Veuillez vous connecter pour accéder à cette page."
login_manager.login_message_category = "warning"


# ==================================================================================================
# ÉTAPE 2 — BASE DE DONNÉES (modèles)
# ==================================================================================================

def utcnow():
    """Date et heure UTC (Conakry est à UTC+0)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now():
    return utcnow()


# ---------------------------------------------------------------- Multi-entreprises
class TenantMixin:
    """Toute table qui appartient à une entreprise (isolation automatique)."""

    @db.declared_attr
    def company_id(cls):
        return db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)


class Company(db.Model):
    """Une entreprise cliente de la plateforme ANSARTECH 224."""
    __tablename__ = "companies"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(30), unique=True, nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    activity = db.Column(db.String(150))
    # Adresse principale (siège)
    address = db.Column(db.String(255))
    city = db.Column(db.String(80), default="Conakry")
    country = db.Column(db.String(80), default="Guinée")
    phone = db.Column(db.String(60))
    email = db.Column(db.String(120))
    website = db.Column(db.String(150))
    rccm = db.Column(db.String(80))
    nif = db.Column(db.String(80))
    # Logo (stocké en base)
    logo = db.Column(db.LargeBinary)
    logo_mimetype = db.Column(db.String(60))
    # Abonnement
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    subscription_end = db.Column(db.Date)
    max_users = db.Column(db.Integer, default=10, nullable=False)
    contact_name = db.Column(db.String(120))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=now)

    @property
    def is_expired(self):
        return bool(self.subscription_end and self.subscription_end < utcnow().date())

    @property
    def can_login(self):
        return self.is_active and not self.is_expired

    @property
    def full_address(self):
        parts = [self.address, self.city, self.country]
        return ", ".join(p for p in parts if p)


TENANT_ROLES = {
    "admin": "Administrateur",
    "gestionnaire": "Gestionnaire",
    "vendeur": "Vendeur",
}

# ---------------------------------------------------------------- Utilisateurs
ROLES = {
    "admin": "Administrateur",
    "gestionnaire": "Gestionnaire",
    "vendeur": "Vendeur",
    "superadmin": "Super-admin plateforme",
}


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    # vide uniquement pour le super-administrateur de la plateforme
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), index=True)
    company = db.relationship("Company")
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(30))
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="vendeur")
    is_active_account = db.Column(db.Boolean, default=True, nullable=False)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)
    failed_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime)
    last_login = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=now)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):
        return self.is_active_account

    @property
    def effective_role(self):
        """Le super-administrateur agit comme administrateur dans l'espace d'une entreprise (support)."""
        if self.role == "superadmin":
            from flask import g, has_request_context
            if has_request_context() and getattr(g, "company_id", None):
                return "admin"
        return self.role

    @property
    def is_superadmin(self):
        return self.role == "superadmin"

    @property
    def role_label(self):
        if self.role == "superadmin" and self.effective_role == "admin":
            return "Super-admin (support)"
        return ROLES.get(self.role, self.role)

    def has_role(self, *roles):
        return self.effective_role in roles


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------------------------------------------------------------- Produits
class Category(TenantMixin, db.Model):
    __tablename__ = "categories"
    __table_args__ = (db.UniqueConstraint("company_id", "name"),)
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    products = db.relationship("Product", back_populates="category")


class Product(TenantMixin, db.Model):
    __tablename__ = "products"
    __table_args__ = (db.UniqueConstraint("company_id", "sku"),)
    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(40), nullable=False, index=True)
    barcode = db.Column(db.String(60))
    name = db.Column(db.String(150), nullable=False, index=True)
    description = db.Column(db.Text)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    category = db.relationship("Category", back_populates="products")
    unit = db.Column(db.String(20), default="pièce")
    purchase_price = db.Column(db.Float, default=0, nullable=False)
    sale_price = db.Column(db.Float, default=0, nullable=False)
    quantity = db.Column(db.Float, default=0, nullable=False)
    min_stock = db.Column(db.Float, default=5, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now, onupdate=now)

    @property
    def stock_value(self):
        return (self.quantity or 0) * (self.purchase_price or 0)

    @property
    def margin(self):
        return (self.sale_price or 0) - (self.purchase_price or 0)

    @property
    def margin_rate(self):
        if not self.sale_price:
            return 0
        return self.margin / self.sale_price * 100

    @property
    def stock_status(self):
        if self.quantity <= 0:
            return "rupture"
        if self.quantity <= self.min_stock:
            return "bas"
        return "ok"


# ---------------------------------------------------------------- Stocks
MOVEMENT_TYPES = {
    "entree": "Entrée",
    "sortie": "Sortie",
    "ajustement": "Ajustement",
    "achat": "Réception achat",
    "vente": "Vente",
    "retour": "Retour / annulation",
}


class StockMovement(TenantMixin, db.Model):
    __tablename__ = "stock_movements"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    product = db.relationship("Product")
    movement_type = db.Column(db.String(20), nullable=False)
    quantity = db.Column(db.Float, nullable=False)  # positif = entrée, négatif = sortie
    stock_after = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(255))
    reference = db.Column(db.String(60))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship("User")
    created_at = db.Column(db.DateTime, default=now, index=True)

    @property
    def type_label(self):
        return MOVEMENT_TYPES.get(self.movement_type, self.movement_type)


# ---------------------------------------------------------------- Achats
class Supplier(TenantMixin, db.Model):
    __tablename__ = "suppliers"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    contact = db.Column(db.String(120))
    phone = db.Column(db.String(30))
    email = db.Column(db.String(120))
    address = db.Column(db.String(255))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=now)
    purchases = db.relationship("Purchase", back_populates="supplier")


PURCHASE_STATUS = {"commande": "Commandé", "recu": "Reçu", "annule": "Annulé"}


class Purchase(TenantMixin, db.Model):
    __tablename__ = "purchases"
    __table_args__ = (db.UniqueConstraint("company_id", "reference"),)
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(40), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    supplier = db.relationship("Supplier", back_populates="purchases")
    status = db.Column(db.String(20), default="commande", nullable=False)
    total = db.Column(db.Float, default=0, nullable=False)
    amount_paid = db.Column(db.Float, default=0, nullable=False)
    notes = db.Column(db.Text)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship("User")
    created_at = db.Column(db.DateTime, default=now, index=True)
    received_at = db.Column(db.DateTime)
    items = db.relationship("PurchaseItem", back_populates="purchase", cascade="all, delete-orphan")

    @property
    def status_label(self):
        return PURCHASE_STATUS.get(self.status, self.status)

    @property
    def balance(self):
        return max(self.total - self.amount_paid, 0)


class PurchaseItem(db.Model):
    __tablename__ = "purchase_items"
    id = db.Column(db.Integer, primary_key=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey("purchases.id"), nullable=False)
    purchase = db.relationship("Purchase", back_populates="items")
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    product = db.relationship("Product")
    quantity = db.Column(db.Float, nullable=False)
    unit_cost = db.Column(db.Float, nullable=False)

    @property
    def line_total(self):
        return self.quantity * self.unit_cost


# ---------------------------------------------------------------- Ventes
class Customer(TenantMixin, db.Model):
    __tablename__ = "customers"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(30))
    email = db.Column(db.String(120))
    address = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=now)
    sales = db.relationship("Sale", back_populates="customer")


SALE_STATUS = {"payee": "Payée", "partielle": "Partielle", "impayee": "Impayée", "annulee": "Annulée"}
PAYMENT_METHODS = {
    "especes": "Espèces",
    "orange_money": "Orange Money",
    "mtn_momo": "MTN Mobile Money",
    "carte": "Carte bancaire",
    "virement": "Virement",
    "credit": "Crédit client",
}


class Sale(TenantMixin, db.Model):
    __tablename__ = "sales"
    __table_args__ = (db.UniqueConstraint("company_id", "invoice_number"),)
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(40), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))
    customer = db.relationship("Customer", back_populates="sales")
    subtotal = db.Column(db.Float, default=0, nullable=False)
    discount = db.Column(db.Float, default=0, nullable=False)
    tax_rate = db.Column(db.Float, default=0, nullable=False)
    tax_amount = db.Column(db.Float, default=0, nullable=False)
    total = db.Column(db.Float, default=0, nullable=False)
    amount_paid = db.Column(db.Float, default=0, nullable=False)
    payment_method = db.Column(db.String(30), default="especes")
    status = db.Column(db.String(20), default="payee", nullable=False)
    notes = db.Column(db.Text)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship("User")
    created_at = db.Column(db.DateTime, default=now, index=True)
    items = db.relationship("SaleItem", back_populates="sale", cascade="all, delete-orphan")

    @property
    def status_label(self):
        return SALE_STATUS.get(self.status, self.status)

    @property
    def payment_label(self):
        return PAYMENT_METHODS.get(self.payment_method, self.payment_method)

    @property
    def balance(self):
        return max(self.total - self.amount_paid, 0)

    @property
    def profit(self):
        cost = sum(i.quantity * i.unit_cost for i in self.items)
        return self.subtotal - self.discount - cost


class SaleItem(db.Model):
    __tablename__ = "sale_items"
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey("sales.id"), nullable=False)
    sale = db.relationship("Sale", back_populates="items")
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    product = db.relationship("Product")
    quantity = db.Column(db.Float, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    unit_cost = db.Column(db.Float, default=0, nullable=False)  # coût d'achat figé au moment de la vente

    @property
    def line_total(self):
        return self.quantity * self.unit_price


# ---------------------------------------------------------------- Archives & échanges
DOC_CATEGORIES = {
    "facture": "Facture",
    "bon_commande": "Bon de commande",
    "contrat": "Contrat",
    "administratif": "Administratif",
    "rapport": "Rapport",
    "autre": "Autre",
}


class Document(TenantMixin, db.Model):
    __tablename__ = "documents"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(30), default="autre")
    description = db.Column(db.Text)
    original_name = db.Column(db.String(255), nullable=False)
    mimetype = db.Column(db.String(120))
    size = db.Column(db.Integer, default=0)
    data = db.Column(db.LargeBinary, nullable=False)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    uploader = db.relationship("User")
    created_at = db.Column(db.DateTime, default=now, index=True)

    @property
    def category_label(self):
        return DOC_CATEGORIES.get(self.category, self.category)


class Message(TenantMixin, db.Model):
    __tablename__ = "messages"
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    sender = db.relationship("User", foreign_keys=[sender_id])
    recipient_id = db.Column(db.Integer, db.ForeignKey("users.id"))  # vide = tout le monde
    recipient = db.relationship("User", foreign_keys=[recipient_id])
    subject = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=now, index=True)


# ---------------------------------------------------------------- Paramètres & sécurité
class Setting(TenantMixin, db.Model):
    __tablename__ = "settings"
    __table_args__ = (db.UniqueConstraint("company_id", "key"),)
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(60), nullable=False)
    value = db.Column(db.Text)


DEFAULT_SETTINGS = {
    "currency": "GNF",
    "tax_rate": "0",
    "invoice_prefix": "FAC",
    "purchase_prefix": "ACH",
    "invoice_title": "FACTURE",
    "invoice_address": "",
    "invoice_phone": "",
    "invoice_email": "",
    "invoice_payment_info": "",
    "invoice_terms": "",
    "invoice_footer": "Merci pour votre confiance. Les marchandises vendues ne sont ni reprises ni échangées.",
    "invoice_color": "#0b5d43",
    "show_logo_on_invoice": "1",
    "low_stock_alert": "1",
}

# Clés tirées de la fiche entreprise (fusionnées dans les paramètres)
COMPANY_FIELDS = ("name", "activity", "address", "city", "country", "phone", "email",
                  "website", "rccm", "nif")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship("User")
    action = db.Column(db.String(80), nullable=False)
    details = db.Column(db.String(500))
    ip_address = db.Column(db.String(60))
    created_at = db.Column(db.DateTime, default=now, index=True)


# ---------------------------------------------------------------- Facture Pro (indépendante du stock)
PRO_DOC_TYPES = {
    "facture": ("FACTURE", "FP"),
    "proforma": ("FACTURE PROFORMA", "PRO"),
    "devis": ("DEVIS", "DEV"),
}


class ProInvoice(TenantMixin, db.Model):
    """Facture saisie librement : client, adresse et lignes tapés à la main."""
    __tablename__ = "pro_invoices"
    __table_args__ = (db.UniqueConstraint("company_id", "number"),)
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), nullable=False)
    doc_type = db.Column(db.String(20), default="facture", nullable=False)
    issue_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date)
    client_name = db.Column(db.String(150), nullable=False)
    client_address = db.Column(db.Text)
    client_phone = db.Column(db.String(40))
    client_email = db.Column(db.String(120))
    client_nif = db.Column(db.String(80))
    payment_method = db.Column(db.String(120))
    discount = db.Column(db.Float, default=0, nullable=False)
    tax_rate = db.Column(db.Float, default=0, nullable=False)
    amount_paid = db.Column(db.Float, default=0, nullable=False)
    notes = db.Column(db.Text)
    share_token = db.Column(db.String(64), unique=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship("User")
    created_at = db.Column(db.DateTime, default=now, index=True)
    updated_at = db.Column(db.DateTime, default=now, onupdate=now)
    lines = db.relationship("ProInvoiceLine", back_populates="invoice", cascade="all, delete-orphan",
                            order_by="ProInvoiceLine.position")

    @property
    def title(self):
        return PRO_DOC_TYPES.get(self.doc_type, PRO_DOC_TYPES["facture"])[0]

    @property
    def subtotal(self):
        return round(sum(line.total for line in self.lines), 2)

    @property
    def taxable(self):
        return max(self.subtotal - (self.discount or 0), 0)

    @property
    def tax_amount(self):
        return round(self.taxable * (self.tax_rate or 0) / 100, 2)

    @property
    def total(self):
        return round(self.taxable + self.tax_amount, 2)

    @property
    def balance(self):
        return max(round(self.total - (self.amount_paid or 0), 2), 0)


class ProInvoiceLine(db.Model):
    __tablename__ = "pro_invoice_lines"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("pro_invoices.id"), nullable=False, index=True)
    invoice = db.relationship("ProInvoice", back_populates="lines")
    position = db.Column(db.Integer, default=0, nullable=False)
    designation = db.Column(db.Text, nullable=False)
    quantity = db.Column(db.Float, default=1, nullable=False)
    unit_price = db.Column(db.Float, default=0, nullable=False)

    @property
    def total(self):
        return round((self.quantity or 0) * (self.unit_price or 0), 2)


# ==================================================================================================
# MULTI-ENTREPRISES (isolation automatique des données)
# ==================================================================================================

_forced_company = ContextVar("forced_company", default=None)


def current_company_id():
    forced = _forced_company.get()
    if forced is not None:
        return forced
    if has_request_context():
        return getattr(g, "company_id", None)
    return None


@contextmanager
def tenant_context(company_id):
    """Exécute un bloc de code au nom d'une entreprise (initialisation, tests, CLI)."""
    token = _forced_company.set(company_id)
    try:
        yield
    finally:
        _forced_company.reset(token)


_installed = False


def init_tenancy():
    global _installed
    if _installed:
        return
    _installed = True

    @event.listens_for(Session, "do_orm_execute")
    def _scope_to_company(state):
        if not (state.is_select or state.is_update or state.is_delete):
            return
        if state.execution_options.get("skip_tenant"):
            return
        company_id = current_company_id()
        if company_id is None:
            return
        state.statement = state.statement.options(
            with_loader_criteria(TenantMixin, lambda cls: cls.company_id == company_id, include_aliases=True),
            with_loader_criteria(User, lambda cls: cls.company_id == company_id, include_aliases=True),
            with_loader_criteria(AuditLog, lambda cls: cls.company_id == company_id, include_aliases=True),
        )

    @event.listens_for(Session, "before_flush")
    def _assign_company(session, _context, _instances):
        company_id = current_company_id()
        for obj in session.new:
            if isinstance(obj, TenantMixin) and obj.company_id is None:
                if company_id is None:
                    raise RuntimeError(f"Aucune entreprise active pour enregistrer {type(obj).__name__}")
                obj.company_id = company_id
            elif isinstance(obj, (User, AuditLog)) and obj.company_id is None and company_id is not None:
                if not (isinstance(obj, User) and obj.role == "superadmin"):
                    obj.company_id = company_id


# ==================================================================================================
# FONCTIONS UTILITAIRES
# ==================================================================================================

# ---------------------------------------------------------------- Rôles
def role_required(*roles):
    """Restreint une page aux rôles donnés (l'administrateur de l'entreprise a toujours accès)."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("auth.login", next=request.path))
            role = current_user.effective_role
            if role != "admin" and role not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def superadmin_required(view):
    """Réservé au propriétaire de la plateforme."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.path))
        if current_user.role != "superadmin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------- Journal d'audit
def audit(action, details="", company_id=None):
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    ip = ip.split(",")[0].strip()[:60]
    user_id = current_user.id if current_user and current_user.is_authenticated else None
    if company_id is None:
        company_id = current_company_id()
        if company_id is None and current_user and current_user.is_authenticated:
            company_id = current_user.company_id
    db.session.add(AuditLog(user_id=user_id, company_id=company_id, action=action[:80],
                            details=(details or "")[:500], ip_address=ip))


# ---------------------------------------------------------------- Entreprise et paramètres
def current_company():
    company_id = current_company_id()
    return db.session.get(Company, company_id) if company_id else None


def get_setting(key):
    row = Setting.query.filter_by(key=key).first() if current_company_id() else None
    if row is None or row.value is None:
        return DEFAULT_SETTINGS.get(key, "")
    return row.value


def all_settings():
    """Paramètres de l'entreprise active, avec sa fiche (nom, adresses…)."""
    data = dict(DEFAULT_SETTINGS)
    company = current_company()
    if company is None:
        data.update(company_name="ANSARTECH 224", company_address="", company_phone="",
                    company_email="", company_rccm="")
        return data
    for row in Setting.query.all():
        data[row.key] = row.value or ""
    for field in COMPANY_FIELDS:
        data["company_" + field] = getattr(company, field) or ""
    data["company_address"] = company.full_address
    data["company_street"] = company.address or ""
    # Coordonnées imprimées sur la facture (par défaut : celles de l'entreprise)
    data["invoice_address_display"] = data["invoice_address"] or company.full_address
    data["invoice_phone_display"] = data["invoice_phone"] or (company.phone or "")
    data["invoice_email_display"] = data["invoice_email"] or (company.email or "")
    return data


def set_setting(key, value):
    row = Setting.query.filter_by(key=key).first()
    if row is None:
        db.session.add(Setting(key=key, value=value))
    else:
        row.value = value


# ---------------------------------------------------------------- Saisie
def to_float(value, default=0.0):
    if value is None:
        return default
    text = str(value).replace(" ", "").replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- Numérotation
def next_reference(model, field, prefix):
    """Génère FAC-2026-00001, ACH-2026-00001, etc."""
    year = utcnow().year
    base = f"{prefix}-{year}-"
    column = getattr(model, field)
    last = (db.session.query(column).filter(column.like(base + "%"))
            .order_by(column.desc()).first())
    number = 1
    if last and last[0]:
        try:
            number = int(last[0].rsplit("-", 1)[1]) + 1
        except (IndexError, ValueError):
            number = model.query.count() + 1
    return f"{base}{number:05d}"


# ---------------------------------------------------------------- Stock
def move_stock(product, quantity, movement_type, reason="", reference=""):
    """Modifie le stock d'un produit et enregistre le mouvement.

    quantity > 0 : entrée ; quantity < 0 : sortie.
    """
    product.quantity = round((product.quantity or 0) + quantity, 3)
    movement = StockMovement(
        product=product,
        movement_type=movement_type,
        quantity=quantity,
        stock_after=product.quantity,
        reason=reason[:255] if reason else "",
        reference=reference[:60] if reference else "",
        user_id=current_user.id if current_user and current_user.is_authenticated else None,
    )
    db.session.add(movement)
    return movement


# ---------------------------------------------------------------- Export CSV
def csv_response(filename, headers, rows):
    buffer = io.StringIO()
    buffer.write("﻿")  # BOM pour qu'Excel lise bien les accents
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return Response(
        buffer.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def flash_errors(errors):
    for error in errors:
        flash(error, "danger")


def money(value, currency=None):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0
    text = f"{number:,.0f}".replace(",", " ")
    if currency is None:
        currency = get_setting("currency")
    return f"{text} {currency}".strip()


def number(value):
    try:
        number_value = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    if number_value == int(number_value):
        return f"{int(number_value):,}".replace(",", " ")
    return f"{number_value:,.2f}".replace(",", " ")


# ==================================================================================================
# INITIALISATION, CRÉATION D'ENTREPRISE, DÉMONSTRATION
# ==================================================================================================

DEFAULT_CATEGORIES = ("Informatique", "Téléphonie", "Accessoires", "Électroménager", "Divers")


def _schema_is_outdated():
    """Vrai si la base vient d'une ancienne version (avant le multi-entreprises)."""
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    if "users" not in tables:
        return False
    columns = {c["name"] for c in inspector.get_columns("users")}
    return "company_id" not in columns or "companies" not in tables


def _reset_database():
    """Supprime toutes les tables existantes (y compris d'anciennes versions)."""
    from sqlalchemy import MetaData
    meta = MetaData()
    meta.reflect(bind=db.engine)
    meta.drop_all(bind=db.engine)


def init_database(app):
    app.config["DB_SCHEMA_ERROR"] = None
    if os.environ.get("RESET_DATABASE") == "1":
        app.logger.warning("RESET_DATABASE=1 : toutes les tables sont supprimées puis recréées.")
        _reset_database()
    elif _schema_is_outdated():
        message = ("La base de données provient d'une ancienne version d'ANSARTECH 224. "
                   "Sur Render, ajoutez la variable RESET_DATABASE=1, redéployez, connectez-vous, "
                   "puis supprimez cette variable (attention : les anciennes données sont effacées).")
        app.config["DB_SCHEMA_ERROR"] = message
        app.logger.error(message)
        return

    try:
        db.create_all()
    except (OperationalError, ProgrammingError, IntegrityError):
        # Un autre processus vient de créer les tables : on continue
        db.session.rollback()

    username = app.config["ADMIN_USERNAME"].strip().lower()
    password = app.config["ADMIN_PASSWORD"]
    owner = User.query.filter_by(role="superadmin").order_by(User.id).first()
    if owner is None:
        taken = User.query.filter(db.func.lower(User.username) == username).first()
        if taken is not None:
            # l'identifiant est pris par un utilisateur d'entreprise : on le libère
            taken.username = f"{taken.username}-{taken.id}"
        owner = User(username=username, full_name="Super-administrateur", email=app.config["ADMIN_EMAIL"],
                     role="superadmin", company_id=None, must_change_password=not app.config.get("TESTING"))
        owner.set_password(password)
        db.session.add(owner)
        db.session.commit()
        app.logger.warning("Compte super-admin créé : identifiant « %s ».", username)
    elif os.environ.get("RESET_ADMIN_PASSWORD") == "1":
        # Récupération sans Shell : remet l'identifiant et le mot de passe de ADMIN_USERNAME / ADMIN_PASSWORD
        taken = User.query.filter(db.func.lower(User.username) == username, User.id != owner.id).first()
        if taken is not None:
            taken.username = f"{taken.username}-{taken.id}"
        owner.username = username
        owner.set_password(password)
        owner.failed_attempts = 0
        owner.locked_until = None
        owner.is_active_account = True
        owner.must_change_password = False
        db.session.commit()
        app.logger.warning("RESET_ADMIN_PASSWORD=1 : compte super-admin « %s » réinitialisé.", username)

    if os.environ.get("DEMO_DATA") == "1" and not Company.query.filter_by(code="demo").first():
        create_demo_company()


def unique_company_code(name):
    """Fabrique un code court et unique à partir du nom (ex. « boutique-kaloum »)."""
    import re
    import unicodedata
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:24] or "entreprise"
    code, n = base, 2
    while Company.query.filter_by(code=code).first():
        code = f"{base}-{n}"
        n += 1
    return code


def create_company(name, admin_username, admin_password, admin_full_name="Administrateur",
                   must_change_password=True, **fields):
    """Crée une entreprise, ses catégories par défaut et son administrateur."""
    company = Company(name=name, code=fields.pop("code", None) or unique_company_code(name), **fields)
    db.session.add(company)
    db.session.flush()
    admin = User(username=admin_username.lower(), full_name=admin_full_name, role="admin",
                 company_id=company.id, must_change_password=must_change_password)
    admin.set_password(admin_password)
    db.session.add(admin)
    with tenant_context(company.id):
        for category in DEFAULT_CATEGORIES:
            db.session.add(Category(name=category, company_id=company.id))
        db.session.flush()
    return company, admin


def create_demo_company():
    """Entreprise de démonstration : identifiant demo / Demo@2026."""
    company, _ = create_company(
        "Boutique Démo Kaloum", "demo", "Demo@2026", "Gérant Démo", must_change_password=False,
        code="demo", activity="Informatique et téléphonie", address="Avenue de la République, Kaloum",
        phone="+224 620 00 00 00", email="demo@ansartech224.com")
    db.session.commit()
    load_demo_data(company.id)
    return company


def load_demo_data(company_id):
    """Remplit une entreprise avec des exemples réalistes (Conakry)."""
    with tenant_context(company_id):
        _load_demo_data()


def _load_demo_data():
    if Product.query.count() > 0:
        return
    admin = User.query.filter_by(role="admin").first()
    categories = {c.name: c for c in Category.query.all()}

    suppliers = [
        Supplier(name="Guinée Distribution SARL", contact="M. Camara", phone="+224 622 11 22 33",
                 address="Kaloum, Conakry"),
        Supplier(name="Madina Import-Export", contact="Mme Diallo", phone="+224 664 44 55 66",
                 address="Marché Madina, Conakry"),
        Supplier(name="Tech Africa Supply", contact="M. Bah", phone="+224 655 77 88 99",
                 email="ventes@techafrica.gn", address="Dixinn, Conakry"),
    ]
    db.session.add_all(suppliers)

    catalogue = [
        ("ORD-001", "Ordinateur portable HP 15", "Informatique", 4_500_000, 5_600_000, 8, 3),
        ("ORD-002", "Ordinateur portable Lenovo IdeaPad", "Informatique", 4_000_000, 5_000_000, 5, 3),
        ("IMP-001", "Imprimante Canon Pixma", "Informatique", 1_200_000, 1_550_000, 4, 2),
        ("TEL-001", "Smartphone Samsung Galaxy A15", "Téléphonie", 1_500_000, 1_900_000, 15, 5),
        ("TEL-002", "Smartphone Tecno Spark 20", "Téléphonie", 900_000, 1_150_000, 22, 6),
        ("TEL-003", "Téléphone Itel basique", "Téléphonie", 120_000, 175_000, 2, 10),
        ("ACC-001", "Chargeur rapide USB-C", "Accessoires", 45_000, 75_000, 60, 15),
        ("ACC-002", "Écouteurs Bluetooth", "Accessoires", 80_000, 130_000, 30, 10),
        ("ACC-003", "Clé USB 64 Go", "Accessoires", 60_000, 95_000, 0, 10),
        ("ACC-004", "Batterie externe 20000 mAh", "Accessoires", 150_000, 225_000, 12, 5),
        ("ELM-001", "Ventilateur sur pied", "Électroménager", 350_000, 475_000, 9, 4),
        ("ELM-002", "Régulateur de tension 2000VA", "Électroménager", 400_000, 560_000, 6, 3),
    ]
    products = []
    for sku, name, cat, cost, price, qty, min_qty in catalogue:
        product = Product(sku=sku, name=name, category=categories.get(cat), purchase_price=cost,
                          sale_price=price, quantity=qty, min_stock=min_qty)
        products.append(product)
        db.session.add(product)
    db.session.flush()

    for product in products:
        db.session.add(StockMovement(product=product, movement_type="entree", quantity=product.quantity,
                                     stock_after=product.quantity, reason="Stock initial", user=admin))

    customers = [
        Customer(name="Mamadou Sow", phone="+224 620 10 20 30", address="Ratoma"),
        Customer(name="Fatoumata Barry", phone="+224 657 40 50 60", address="Matam"),
        Customer(name="Entreprise BTP Kindia", phone="+224 628 70 80 90", address="Kindia"),
    ]
    db.session.add_all(customers)

    purchase = Purchase(reference="ACH-DEMO-00001", supplier=suppliers[2], status="recu",
                        user=admin, created_at=utcnow() - timedelta(days=40),
                        received_at=utcnow() - timedelta(days=38))
    purchase.items.append(PurchaseItem(product=products[0], quantity=5, unit_cost=4_500_000))
    purchase.items.append(PurchaseItem(product=products[3], quantity=10, unit_cost=1_500_000))
    purchase.total = sum(i.line_total for i in purchase.items)
    purchase.amount_paid = purchase.total
    db.session.add(purchase)

    rng = random.Random(224)
    sellable = [p for p in products if p.sku != "ACC-003"]
    for index in range(45):
        day = utcnow() - timedelta(days=rng.randint(0, 89), hours=rng.randint(0, 10))
        sale = Sale(invoice_number=f"FAC-DEMO-{index + 1:05d}",
                    customer=rng.choice(customers + [None, None]),
                    payment_method=rng.choice(["especes", "orange_money", "mtn_momo", "especes"]),
                    user=admin, created_at=day)
        for product in rng.sample(sellable, rng.randint(1, 3)):
            sale.items.append(SaleItem(product=product, quantity=rng.randint(1, 3),
                                       unit_price=product.sale_price, unit_cost=product.purchase_price))
        sale.subtotal = sum(i.line_total for i in sale.items)
        sale.total = sale.subtotal
        sale.amount_paid = sale.total
        sale.status = "payee"
        db.session.add(sale)
    db.session.commit()


# ==================================================================================================
# PDF PROFESSIONNEL ET MONTANT EN LETTRES
# ==================================================================================================

# ---------------------------------------------------------------- Montant en lettres
_UNITS = ["zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf", "dix",
          "onze", "douze", "treize", "quatorze", "quinze", "seize"]
_TENS = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante"}


def _below_100(n):
    if n < 17:
        return _UNITS[n]
    if n < 20:
        return "dix-" + _UNITS[n - 10]
    tens, unit = divmod(n, 10)
    if tens in _TENS:
        if unit == 0:
            return _TENS[tens]
        if unit == 1:
            return _TENS[tens] + " et un"
        return _TENS[tens] + "-" + _UNITS[unit]
    if tens == 7:
        if n == 71:
            return "soixante et onze"
        return "soixante-" + _below_100(n - 60)
    rest = n - 80
    if rest == 0:
        return "quatre-vingts"
    return "quatre-vingt-" + _below_100(rest)


def _below_1000(n, plural=True):
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        if hundreds == 1:
            parts.append("cent")
        else:
            parts.append(_UNITS[hundreds] + (" cents" if rest == 0 and plural else " cent"))
    if rest:
        words = _below_100(rest)
        if not plural and words == "quatre-vingts":
            words = "quatre-vingt"
        parts.append(words)
    return " ".join(parts)


def number_to_words(n):
    """Écrit un nombre entier en lettres (orthographe traditionnelle française)."""
    n = int(n)
    if n == 0:
        return "zéro"
    if n < 0:
        return "moins " + number_to_words(-n)
    parts = []
    billions, n = divmod(n, 10 ** 9)
    if billions:
        parts.append(f"{number_to_words(billions)} {'milliard' if billions == 1 else 'milliards'}")
    millions, n = divmod(n, 10 ** 6)
    if millions:
        parts.append(f"{number_to_words(millions)} {'million' if millions == 1 else 'millions'}")
    thousands, n = divmod(n, 1000)
    if thousands:
        parts.append("mille" if thousands == 1 else f"{_below_1000(thousands, plural=False)} mille")
    if n:
        parts.append(_below_1000(n))
    return " ".join(parts)


CURRENCY_WORDS = {
    "GNF": ("franc guinéen", "francs guinéens"),
    "FCFA": ("franc CFA", "francs CFA"),
    "EUR": ("euro", "euros"),
    "USD": ("dollar", "dollars"),
}


def amount_in_words(amount, currency="GNF"):
    """Ex. 1 250 000 GNF → « Un million deux cent cinquante mille francs guinéens »."""
    amount = round(float(amount or 0), 2)
    whole = int(amount)
    cents = int(round((amount - whole) * 100))
    singular, plural = CURRENCY_WORDS.get(currency, (currency, currency))
    words = number_to_words(whole)
    link = " de " if whole >= 10 ** 6 and whole % 10 ** 6 == 0 else " "
    text = words + link + (singular if whole <= 1 else plural)
    if cents and currency in ("EUR", "USD"):
        text += f" et {number_to_words(cents)} centime{'s' if cents > 1 else ''}"
    return text[:1].upper() + text[1:]


# ---------------------------------------------------------------- Mise en forme
def _clean(text):
    """Texte sûr pour les polices PDF standard (accents français conservés)."""
    text = str(text or "")
    for bad, good in {" ": " ", "\xa0": " ", " ": " ", "’": "'", "‘": "'",
                      "“": '"', "”": '"', "…": "...", "–": "-", "—": "-"}.items():
        text = text.replace(bad, good)
    text = text.encode("cp1252", errors="replace").decode("cp1252")
    return escape(text)


def _lines_html(text):
    return "<br/>".join(_clean(line) for line in str(text or "").splitlines() if line.strip())


def format_amount(value, currency):
    decimals = 2 if currency in ("EUR", "USD") else 0
    text = f"{float(value or 0):,.{decimals}f}".replace(",", " ")
    return f"{text} {currency}"


def _qty(value):
    value = float(value or 0)
    return f"{int(value):,}".replace(",", " ") if value == int(value) else f"{value:,.2f}".replace(",", " ")


def _tint(hex_color, ratio):
    """Mélange une couleur avec du blanc (ratio 0 = blanc, 1 = couleur pleine)."""
    base = colors.HexColor(hex_color)
    return colors.Color(1 - (1 - base.red) * ratio, 1 - (1 - base.green) * ratio, 1 - (1 - base.blue) * ratio)


class _NumberedCanvas(rl_canvas.Canvas):
    """Ajoute « Page x / y » et le pied de page sur chaque page."""

    def __init__(self, *args, footer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_pages = []
        self._footer = footer

    def showPage(self):
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_pages)
        for state in self._saved_pages:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total):
        footer = self._footer or {}
        width, _ = A4
        self.setStrokeColor(footer.get("color", colors.HexColor("#0b5d43")))
        self.setLineWidth(1.2)
        self.line(18 * mm, 17 * mm, width - 18 * mm, 17 * mm)
        self.setFillColor(colors.HexColor("#555555"))
        self.setFont("Helvetica", 7.5)
        y = 12.5 * mm
        for text in footer.get("lines", []):
            self.drawCentredString(width / 2, y, text)
            y -= 3.6 * mm
        self.setFont("Helvetica", 7)
        self.drawRightString(width - 18 * mm, 20 * mm, f"Page {self._pageNumber} / {total}")


def _footer_text(text):
    return str(text or "").encode("cp1252", errors="replace").decode("cp1252")[:150]


def build_pro_invoice_pdf(invoice):
    """Construit le PDF A4 d'une Facture Pro et renvoie ses octets."""
    with tenant_context(invoice.company_id):
        settings = all_settings()
        company = db.session.get(Company, invoice.company_id)
        return _render_pdf(invoice, settings, company)


def _render_pdf(invoice, s, company):
    currency = s.get("currency") or "GNF"
    hex_color = s.get("invoice_color") or "#0b5d43"
    if len(hex_color) != 7 or not hex_color.startswith("#"):
        hex_color = "#0b5d43"
    brand = colors.HexColor(hex_color)
    light = _tint(hex_color, 0.08)
    soft = _tint(hex_color, 0.16)
    dark_text = colors.HexColor("#1b2420")
    grey = colors.HexColor("#5b6661")

    def style(name, **kw):
        base = dict(fontName="Helvetica", fontSize=9.5, leading=13, textColor=dark_text)
        base.update(kw)
        return ParagraphStyle(name, **base)

    st = {
        "company": style("company", fontName="Helvetica-Bold", fontSize=19, leading=23, alignment=TA_CENTER,
                         textColor=brand),
        "activity": style("activity", fontName="Helvetica-Oblique", fontSize=9.5, alignment=TA_CENTER,
                          textColor=grey),
        "center": style("center", fontSize=8.8, leading=12, alignment=TA_CENTER, textColor=grey),
        "title": style("title", fontName="Helvetica-Bold", fontSize=22, leading=26, alignment=TA_CENTER,
                       textColor=dark_text),
        "number": style("number", fontName="Helvetica-Bold", fontSize=11, leading=14, alignment=TA_CENTER,
                        textColor=brand),
        "box_title": style("box_title", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=brand),
        "box": style("box", fontSize=9.5, leading=13.5),
        "box_strong": style("box_strong", fontName="Helvetica-Bold", fontSize=11, leading=14),
        "th": style("th", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=colors.white),
        "th_r": style("th_r", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=colors.white,
                      alignment=TA_RIGHT),
        "th_c": style("th_c", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=colors.white,
                      alignment=TA_CENTER),
        "td": style("td", fontSize=9.3, leading=12.5),
        "td_r": style("td_r", fontSize=9.3, leading=12.5, alignment=TA_RIGHT),
        "td_c": style("td_c", fontSize=9.3, leading=12.5, alignment=TA_CENTER, textColor=grey),
        "tot_l": style("tot_l", fontSize=9.5, leading=12, textColor=grey),
        "tot_r": style("tot_r", fontSize=9.5, leading=12, alignment=TA_RIGHT),
        "grand_l": style("grand_l", fontName="Helvetica-Bold", fontSize=11.5, leading=14, textColor=colors.white),
        "grand_r": style("grand_r", fontName="Helvetica-Bold", fontSize=11.5, leading=14, textColor=colors.white,
                         alignment=TA_RIGHT),
        "due_l": style("due_l", fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=brand),
        "due_r": style("due_r", fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=brand,
                       alignment=TA_RIGHT),
        "words": style("words", fontSize=9.5, leading=13.5, alignment=TA_CENTER),
        "small_title": style("small_title", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=brand),
        "small": style("small", fontSize=8.8, leading=12, textColor=dark_text),
        "sign": style("sign", fontSize=9, leading=12, alignment=TA_CENTER, textColor=grey),
        "sign_b": style("sign_b", fontName="Helvetica-Bold", fontSize=9.5, leading=12, alignment=TA_CENTER),
    }

    page_width = A4[0] - 36 * mm
    story = []

    # ---------- En-tête centré
    if s.get("show_logo_on_invoice") == "1" and company is not None and company.logo:
        try:
            from PIL import Image as PILImage
            PILImage.open(io.BytesIO(company.logo)).load()  # vérifie que l'image est lisible
            reader = ImageReader(io.BytesIO(company.logo))
            iw, ih = reader.getSize()
            height = 14 * mm
            width = min(height * iw / ih, 60 * mm)
            height = width * ih / iw
            logo = Image(io.BytesIO(company.logo), width=width, height=height)
            logo.hAlign = "CENTER"
            story += [logo, Spacer(1, 2 * mm)]
        except Exception:  # logo illisible : on continue sans
            pass
    story.append(Paragraph(_clean(s.get("company_name")), st["company"]))
    if s.get("company_activity"):
        story.append(Paragraph(_clean(s["company_activity"]), st["activity"]))
    story.append(Spacer(1, 1.5 * mm))
    if s.get("invoice_address_display"):
        address = " · ".join(_clean(line) for line in s["invoice_address_display"].splitlines() if line.strip())
        story.append(Paragraph(address, st["center"]))
    contacts = [f"Tél : {_clean(s['invoice_phone_display'])}" if s.get("invoice_phone_display") else "",
                _clean(s.get("invoice_email_display")), _clean(s.get("company_website"))]
    contacts = [c for c in contacts if c]
    if contacts:
        story.append(Paragraph(" · ".join(contacts), st["center"]))
    legal = [f"RCCM : {_clean(s['company_rccm'])}" if s.get("company_rccm") else "",
             f"NIF : {_clean(s['company_nif'])}" if s.get("company_nif") else ""]
    legal = [x for x in legal if x]
    if legal:
        story.append(Paragraph(" · ".join(legal), st["center"]))
    story.append(Spacer(1, 3 * mm))
    rule = Table([[""]], colWidths=[page_width], rowHeights=[1.6])
    rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), brand)]))
    story += [rule, Spacer(1, 4.5 * mm)]

    # ---------- Titre
    story.append(Paragraph(_clean(invoice.title), st["title"]))
    story.append(Paragraph(f"N° {_clean(invoice.number)}", st["number"]))
    story.append(Spacer(1, 5 * mm))

    # ---------- Client et informations
    client = [Paragraph("FACTURÉ À" if invoice.doc_type != "devis" else "CLIENT", st["box_title"]),
              Spacer(1, 1.5 * mm), Paragraph(_clean(invoice.client_name), st["box_strong"])]
    if invoice.client_address:
        client.append(Paragraph(_lines_html(invoice.client_address), st["box"]))
    if invoice.client_phone:
        client.append(Paragraph(f"Tél : {_clean(invoice.client_phone)}", st["box"]))
    if invoice.client_email:
        client.append(Paragraph(_clean(invoice.client_email), st["box"]))
    if invoice.client_nif:
        client.append(Paragraph(f"NIF : {_clean(invoice.client_nif)}", st["box"]))

    info_rows = [("Date", invoice.issue_date.strftime("%d/%m/%Y"))]
    if invoice.due_date:
        label = "Valable jusqu'au" if invoice.doc_type in ("devis", "proforma") else "Échéance"
        info_rows.append((label, invoice.due_date.strftime("%d/%m/%Y")))
    if invoice.payment_method:
        info_rows.append(("Paiement", invoice.payment_method))
    info_rows.append(("Devise", currency))
    info = [Paragraph("INFORMATIONS", st["box_title"]), Spacer(1, 1.5 * mm)]
    for label, value in info_rows:
        info.append(Paragraph(f"<font color='#5b6661'>{_clean(label)} :</font> <b>{_clean(value)}</b>", st["box"]))

    gap = 6 * mm
    box_w = (page_width - gap) / 2
    boxes = Table([[client, "", info]], colWidths=[box_w, gap, box_w])
    boxes.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, 0), light), ("BACKGROUND", (2, 0), (2, 0), light),
        ("LINEBEFORE", (0, 0), (0, 0), 3, brand), ("LINEBEFORE", (2, 0), (2, 0), 3, brand),
        ("LEFTPADDING", (0, 0), (0, 0), 10), ("LEFTPADDING", (2, 0), (2, 0), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [boxes, Spacer(1, 5.5 * mm)]

    # ---------- Lignes
    widths = [11 * mm, page_width - 11 * mm - 20 * mm - 34 * mm - 36 * mm, 20 * mm, 34 * mm, 36 * mm]
    data = [[Paragraph("N°", st["th_c"]), Paragraph("DÉSIGNATION", st["th"]), Paragraph("QTÉ", st["th_r"]),
             Paragraph("PRIX UNITAIRE", st["th_r"]), Paragraph("MONTANT", st["th_r"])]]
    for index, line in enumerate(invoice.lines, start=1):
        data.append([Paragraph(str(index), st["td_c"]), Paragraph(_lines_html(line.designation), st["td"]),
                     Paragraph(_qty(line.quantity), st["td_r"]),
                     Paragraph(format_amount(line.unit_price, currency), st["td_r"]),
                     Paragraph(f"<b>{format_amount(line.total, currency)}</b>", st["td_r"])])
    lines_table = Table(data, colWidths=widths, repeatRows=1)
    table_style = [
        ("BACKGROUND", (0, 0), (-1, 0), brand),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor("#dfe5e2")),
        ("LINEBELOW", (0, -1), (-1, -1), 1.2, brand),
    ]
    for row in range(1, len(data)):
        if row % 2 == 0:
            table_style.append(("BACKGROUND", (0, row), (-1, row), light))
    lines_table.setStyle(TableStyle(table_style))
    story += [lines_table, Spacer(1, 4 * mm)]

    # ---------- Totaux
    totals = [[Paragraph("Sous-total", st["tot_l"]), Paragraph(format_amount(invoice.subtotal, currency), st["tot_r"])]]
    if invoice.discount:
        totals.append([Paragraph("Remise", st["tot_l"]),
                       Paragraph("- " + format_amount(invoice.discount, currency), st["tot_r"])])
    if invoice.tax_rate:
        totals.append([Paragraph(f"TVA ({invoice.tax_rate:g} %)", st["tot_l"]),
                       Paragraph(format_amount(invoice.tax_amount, currency), st["tot_r"])])
    grand_row = len(totals)
    totals.append([Paragraph("TOTAL" + (" TTC" if invoice.tax_rate else ""), st["grand_l"]),
                   Paragraph(format_amount(invoice.total, currency), st["grand_r"])])
    due_row = None
    if invoice.amount_paid:
        totals.append([Paragraph("Acompte / payé", st["tot_l"]),
                       Paragraph(format_amount(invoice.amount_paid, currency), st["tot_r"])])
        totals.append([Paragraph("Reste à payer", st["due_l"]),
                       Paragraph(format_amount(invoice.balance, currency), st["due_r"])])
        due_row = len(totals) - 1
    totals_table = Table(totals, colWidths=[42 * mm, 46 * mm], hAlign="RIGHT")
    totals_style = [
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, grand_row - 1), 0.4, colors.HexColor("#dfe5e2")),
        ("BACKGROUND", (0, grand_row), (-1, grand_row), brand),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    if due_row is not None:
        totals_style.append(("BACKGROUND", (0, due_row), (-1, due_row), soft))
    totals_table.setStyle(TableStyle(totals_style))

    phrase = {"devis": "Arrêté le présent devis", "proforma": "Arrêtée la présente facture proforma"}.get(
        invoice.doc_type, "Arrêtée la présente facture")
    words = Table([[Paragraph(f"{phrase} à la somme de :<br/><b>{_clean(amount_in_words(invoice.total, currency))}"
                              f"</b> ({_clean(format_amount(invoice.total, currency))})", st["words"])]],
                  colWidths=[page_width])
    words.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), light), ("BOX", (0, 0), (-1, -1), 0.6, soft),
                               ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story.append(KeepTogether([totals_table, Spacer(1, 4.5 * mm), words]))

    # ---------- Notes, paiement, conditions
    extras = []
    if invoice.notes:
        extras.append(("NOTES", invoice.notes))
    if s.get("invoice_payment_info"):
        extras.append(("MOYENS DE PAIEMENT", s["invoice_payment_info"]))
    if s.get("invoice_terms"):
        extras.append(("CONDITIONS", s["invoice_terms"]))
    # ---------- Bas de facture : informations à gauche, signature et cachet à droite
    left = []
    for title, text in extras:
        block = Table([[[Paragraph(title, st["small_title"]), Spacer(1, 0.8 * mm),
                         Paragraph(_lines_html(text), st["small"])]]], colWidths=[page_width - 72 * mm])
        block.setStyle(TableStyle([("LINEBEFORE", (0, 0), (0, 0), 2.5, brand),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 2),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        left += [block, Spacer(1, 2.5 * mm)]
    signature = Table([[Paragraph(f"Pour <b>{_clean(s.get('company_name'))}</b>", st["sign"])],
                       [""],
                       [Paragraph("Signature et cachet", st["sign"])]],
                      colWidths=[62 * mm], rowHeights=[None, 17 * mm, None])
    signature.setStyle(TableStyle([("LINEABOVE", (0, 2), (0, 2), 0.6, grey),
                                   ("BOX", (0, 0), (-1, -1), 0.4, soft)]))
    bottom = Table([[left or "", "", signature]], colWidths=[page_width - 72 * mm, 10 * mm, 62 * mm])
    bottom.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [Spacer(1, 5 * mm), KeepTogether([bottom])]

    # ---------- Pied de page
    footer_lines = []
    if s.get("invoice_footer"):
        footer_lines.append(_footer_text(s["invoice_footer"]))
    footer_lines.append(_footer_text(" · ".join(x for x in [s.get("company_name"), s.get("company_address"),
                                                             s.get("invoice_phone_display")] if x)))
    footer = {"color": brand, "lines": footer_lines[:2]}

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=12 * mm,
                            bottomMargin=23 * mm, title=f"{invoice.title} {invoice.number}",
                            author=str(s.get("company_name") or ""), subject=f"{invoice.title} - {invoice.client_name}",
                            creator="ANSARTECH 224")
    doc.build(story, canvasmaker=lambda *a, **k: _NumberedCanvas(*a, footer=footer, **k))
    return buffer.getvalue()


# ==================================================================================================
# ÉTAPE 3 — CONNEXION ET UTILISATEURS
# ==================================================================================================

auth_bp = Blueprint("auth", __name__)


def password_errors(password):
    errors = []
    if len(password) < 8:
        errors.append("Le mot de passe doit contenir au moins 8 caractères.")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        errors.append("Le mot de passe doit contenir des lettres et des chiffres.")
    return errors


def _safe_next(target):
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.netloc or parsed.scheme or not target.startswith("/") or target.startswith("//"):
        return None
    return target


@auth_bp.route("/connexion", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    if request.method == "POST" and current_app.config.get("DB_SCHEMA_ERROR"):
        return render_template("auth/login.html")
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter(db.func.lower(User.username) == username).first()
        error = "Identifiant ou mot de passe incorrect."

        if user and user.locked_until and user.locked_until > utcnow():
            minutes = int((user.locked_until - utcnow()).total_seconds() // 60) + 1
            flash(f"Compte temporairement bloqué. Réessayez dans {minutes} minute(s).", "danger")
            return render_template("auth/login.html", username=username)

        # tolère les espaces copiés-collés par erreur avant/après le mot de passe
        if user and (user.check_password(password) or
                     (password != password.strip() and user.check_password(password.strip()))):
            if not user.is_active_account:
                flash("Ce compte est désactivé. Contactez l'administrateur.", "danger")
                return render_template("auth/login.html", username=username)
            if user.role != "superadmin":
                company = db.session.get(Company, user.company_id) if user.company_id else None
                if company is None or not company.can_login:
                    reason = "expiré" if company is not None and company.is_expired else "suspendu"
                    flash(f"L'abonnement de votre entreprise est {reason}. Contactez ANSARTECH 224.", "danger")
                    return render_template("auth/login.html", username=username)
            user.failed_attempts = 0
            user.locked_until = None
            user.last_login = utcnow()
            login_user(user, remember=bool(request.form.get("remember")))
            session.pop("support_company_id", None)
            audit("connexion", f"Connexion de {user.username}", company_id=user.company_id)
            db.session.commit()
            home = url_for("platform.index") if user.role == "superadmin" else url_for("dashboard.index")
            return redirect(_safe_next(request.args.get("next")) or home)

        if user:
            user.failed_attempts = (user.failed_attempts or 0) + 1
            if user.failed_attempts >= current_app.config["MAX_LOGIN_ATTEMPTS"]:
                user.locked_until = utcnow() + timedelta(
                    minutes=current_app.config["LOCKOUT_MINUTES"])
                user.failed_attempts = 0
                error = (f"Trop d'essais. Compte bloqué {current_app.config['LOCKOUT_MINUTES']} minutes.")
            audit("echec_connexion", f"Échec pour {username}", company_id=user.company_id)
            db.session.commit()
        flash(error, "danger")
        return render_template("auth/login.html", username=username)
    return render_template("auth/login.html")


@auth_bp.route("/deconnexion", methods=["POST"])
@login_required
def logout():
    audit("deconnexion", current_user.username)
    db.session.commit()
    session.pop("support_company_id", None)
    logout_user()
    flash("Vous êtes déconnecté.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/mot-de-passe", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        errors = []
        if not current_user.check_password(current):
            errors.append("Le mot de passe actuel est incorrect.")
        if new != confirm:
            errors.append("Les deux nouveaux mots de passe ne correspondent pas.")
        if new == current:
            errors.append("Le nouveau mot de passe doit être différent de l'ancien.")
        errors += password_errors(new)
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            current_user.set_password(new)
            current_user.must_change_password = False
            audit("changement_mot_de_passe", current_user.username)
            db.session.commit()
            flash("Mot de passe modifié avec succès.", "success")
            return redirect(url_for("dashboard.index"))
    return render_template("auth/change_password.html")


@auth_bp.route("/profil", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        if not full_name:
            flash("Le nom complet est obligatoire.", "danger")
        else:
            current_user.full_name = full_name[:120]
            current_user.email = request.form.get("email", "").strip()[:120]
            current_user.phone = request.form.get("phone", "").strip()[:30]
            db.session.commit()
            flash("Profil mis à jour.", "success")
            return redirect(url_for("auth.profile"))
    return render_template("auth/profile.html")


# ---------------------------------------------------------------- Utilisateurs (admin)
def _company_or_platform():
    """La gestion des utilisateurs se fait toujours dans le cadre d'une entreprise."""
    if not g.get("company_id"):
        return redirect(url_for("platform.index"))
    return None


@auth_bp.route("/utilisateurs")
@role_required("admin")
def users():
    redirect_response = _company_or_platform()
    if redirect_response:
        return redirect_response
    company = db.session.get(Company, g.company_id)
    rows = User.query.filter(User.company_id == g.company_id).order_by(User.full_name).all()
    return render_template("auth/users.html", users=rows, roles=TENANT_ROLES, company=company)


@auth_bp.route("/utilisateurs/nouveau", methods=["GET", "POST"])
@auth_bp.route("/utilisateurs/<int:user_id>/modifier", methods=["GET", "POST"])
@role_required("admin")
def user_form(user_id=None):
    redirect_response = _company_or_platform()
    if redirect_response:
        return redirect_response
    user = db.session.get(User, user_id) if user_id else None
    if user_id and (not user or user.company_id != g.company_id):
        abort(404)
    company = db.session.get(Company, g.company_id)
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        full_name = request.form.get("full_name", "").strip()
        role = request.form.get("role", "vendeur")
        password = request.form.get("password", "")
        errors = []
        if not re.fullmatch(r"[a-z0-9._-]{3,50}", username):
            errors.append("Identifiant : 3 à 50 caractères (lettres minuscules, chiffres, . _ -).")
        if not full_name:
            errors.append("Le nom complet est obligatoire.")
        if role not in TENANT_ROLES:
            errors.append("Rôle invalide.")
        # identifiant unique sur toute la plateforme
        existing = User.query.execution_options(skip_tenant=True).filter(
            db.func.lower(User.username) == username).first()
        if existing and (not user or existing.id != user.id):
            errors.append("Cet identifiant est déjà utilisé.")
        if not user or password:
            errors += password_errors(password)
        if user and user.id == current_user.id and role != "admin":
            errors.append("Vous ne pouvez pas retirer votre propre rôle administrateur.")
        if not user and User.query.filter(User.company_id == g.company_id).count() >= company.max_users:
            errors.append(f"Limite atteinte : votre abonnement permet {company.max_users} utilisateur(s).")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("auth/user_form.html", user=user, roles=TENANT_ROLES, form=request.form)

        creating = user is None
        if creating:
            user = User(must_change_password=True, company_id=g.company_id)
            db.session.add(user)
        user.username = username
        user.full_name = full_name[:120]
        user.email = request.form.get("email", "").strip()[:120]
        user.phone = request.form.get("phone", "").strip()[:30]
        user.role = role
        if password:
            user.set_password(password)
            if not creating:
                user.must_change_password = True
        audit("creation_utilisateur" if creating else "modification_utilisateur", username)
        db.session.commit()
        flash("Utilisateur enregistré.", "success")
        return redirect(url_for("auth.users"))
    return render_template("auth/user_form.html", user=user, roles=TENANT_ROLES, form={})


@auth_bp.route("/utilisateurs/<int:user_id>/statut", methods=["POST"])
@role_required("admin")
def toggle_user(user_id):
    redirect_response = _company_or_platform()
    if redirect_response:
        return redirect_response
    user = db.session.get(User, user_id)
    if not user or user.company_id != g.company_id:
        abort(404)
    if user.id == current_user.id:
        flash("Vous ne pouvez pas désactiver votre propre compte.", "danger")
    else:
        user.is_active_account = not user.is_active_account
        user.failed_attempts = 0
        user.locked_until = None
        audit("statut_utilisateur", f"{user.username} -> {'actif' if user.is_active_account else 'inactif'}")
        db.session.commit()
        flash("Statut du compte modifié.", "success")
    return redirect(url_for("auth.users"))


# ==================================================================================================
# ÉTAPE 4 — TABLEAU DE BORD
# ==================================================================================================

dashboard_bp = Blueprint("dashboard", __name__)


def _valid_sales(start, end=None):
    query = Sale.query.filter(Sale.status != "annulee", Sale.created_at >= start)
    if end:
        query = query.filter(Sale.created_at < end)
    return query


@dashboard_bp.route("/", endpoint="index")
@login_required
def dashboard_index():
    now = utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today.replace(day=1)
    chart_start = today - timedelta(days=13)

    today_sales = _valid_sales(today).all()
    month_sales = _valid_sales(month_start).all()
    chart_sales = _valid_sales(chart_start).all()

    products = Product.query.filter_by(is_active=True).all()
    stock_value = sum(p.stock_value for p in products)
    low_stock = sorted([p for p in products if p.quantity <= p.min_stock], key=lambda p: p.quantity)

    daily = {(chart_start + timedelta(days=i)).date(): 0 for i in range(14)}
    for sale in chart_sales:
        day = sale.created_at.date()
        if day in daily:
            daily[day] += sale.total

    receivables = db.session.query(db.func.coalesce(db.func.sum(Sale.total - Sale.amount_paid), 0)).filter(
        Sale.status.in_(["partielle", "impayee"])).scalar()
    pending_purchases = Purchase.query.filter_by(status="commande").count()

    stats = {
        "today_total": sum(s.total for s in today_sales),
        "today_count": len(today_sales),
        "month_total": sum(s.total for s in month_sales),
        "month_profit": sum(s.profit for s in month_sales),
        "stock_value": stock_value,
        "product_count": len(products),
        "receivables": receivables or 0,
        "pending_purchases": pending_purchases,
    }
    recent_sales = Sale.query.order_by(Sale.created_at.desc()).limit(6).all()
    return render_template(
        "dashboard.html",
        stats=stats,
        low_stock=low_stock[:8],
        recent_sales=recent_sales,
        chart_labels=[d.strftime("%d/%m") for d in daily],
        chart_values=[round(v) for v in daily.values()],
    )


# ==================================================================================================
# ÉTAPE 5 — PRODUITS
# ==================================================================================================

products_bp = Blueprint("products", __name__, url_prefix="/produits")


@products_bp.route("/", endpoint="index")
@login_required
def products_index():
    q = request.args.get("q", "").strip()
    category_id = to_int(request.args.get("categorie"))
    status = request.args.get("statut", "")
    page = to_int(request.args.get("page"), 1) or 1

    query = Product.query.filter_by(is_active=(status != "archives"))
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Product.name.ilike(like), Product.sku.ilike(like),
                                    Product.barcode.ilike(like)))
    if category_id:
        query = query.filter(Product.category_id == category_id)
    if status == "bas":
        query = query.filter(Product.quantity <= Product.min_stock, Product.quantity > 0)
    elif status == "rupture":
        query = query.filter(Product.quantity <= 0)

    pagination = query.order_by(Product.name).paginate(page=page, per_page=25, error_out=False)
    return render_template("products/index.html", pagination=pagination, products=pagination.items,
                           categories=Category.query.order_by(Category.name).all(),
                           q=q, category_id=category_id, status=status)


@products_bp.route("/export", endpoint="export")
@login_required
def products_export():
    rows = [(p.sku, p.name, p.category.name if p.category else "", p.unit, p.purchase_price,
             p.sale_price, p.quantity, p.min_stock, p.stock_value)
            for p in Product.query.filter_by(is_active=True).order_by(Product.name)]
    return csv_response("produits.csv", ["Code", "Nom", "Catégorie", "Unité", "Prix achat", "Prix vente",
                                         "Quantité", "Stock min", "Valeur stock"], rows)


@products_bp.route("/<int:product_id>", endpoint="detail")
@login_required
def products_detail(product_id):
    product = db.session.get(Product, product_id) or abort(404)
    movements = (StockMovement.query.filter_by(product_id=product.id)
                 .order_by(StockMovement.created_at.desc()).limit(20).all())
    return render_template("products/detail.html", product=product, movements=movements)


@products_bp.route("/nouveau", methods=["GET", "POST"])
@products_bp.route("/<int:product_id>/modifier", methods=["GET", "POST"])
@role_required("gestionnaire")
def form(product_id=None):
    product = db.session.get(Product, product_id) if product_id else None
    if product_id and not product:
        abort(404)
    categories = Category.query.order_by(Category.name).all()

    if request.method == "POST":
        f = request.form
        sku = f.get("sku", "").strip().upper()
        name = f.get("name", "").strip()
        purchase_price = to_float(f.get("purchase_price"))
        sale_price = to_float(f.get("sale_price"))
        min_stock = to_float(f.get("min_stock"), 0)
        initial_qty = to_float(f.get("quantity"), 0)
        errors = []
        if not sku:
            errors.append("Le code produit est obligatoire.")
        if not name:
            errors.append("Le nom du produit est obligatoire.")
        if purchase_price < 0 or sale_price < 0 or min_stock < 0 or initial_qty < 0:
            errors.append("Les prix et quantités ne peuvent pas être négatifs.")
        duplicate = Product.query.filter(Product.sku == sku).first()
        if duplicate and (not product or duplicate.id != product.id):
            errors.append("Ce code produit existe déjà.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("products/form.html", product=product, categories=categories, form=f)
        if sale_price < purchase_price:
            flash("Attention : le prix de vente est inférieur au prix d'achat.", "warning")

        creating = product is None
        if creating:
            product = Product(quantity=0)
            db.session.add(product)
        product.sku = sku[:40]
        product.name = name[:150]
        product.barcode = f.get("barcode", "").strip()[:60]
        product.description = f.get("description", "").strip()
        product.unit = f.get("unit", "pièce").strip()[:20] or "pièce"
        product.category_id = to_int(f.get("category_id"))
        product.purchase_price = purchase_price
        product.sale_price = sale_price
        product.min_stock = min_stock
        db.session.flush()
        if creating and initial_qty > 0:
            move_stock(product, initial_qty, "entree", "Stock initial")
        audit("creation_produit" if creating else "modification_produit", f"{sku} - {name}")
        db.session.commit()
        flash("Produit enregistré.", "success")
        return redirect(url_for("products.detail", product_id=product.id))

    return render_template("products/form.html", product=product, categories=categories, form={})


@products_bp.route("/<int:product_id>/archiver", methods=["POST"])
@role_required("gestionnaire")
def toggle_archive(product_id):
    product = db.session.get(Product, product_id) or abort(404)
    product.is_active = not product.is_active
    audit("archivage_produit", f"{product.sku} actif={product.is_active}")
    db.session.commit()
    flash("Produit archivé." if not product.is_active else "Produit réactivé.", "success")
    return redirect(url_for("products.detail", product_id=product.id))


@products_bp.route("/<int:product_id>/supprimer", methods=["POST"], endpoint="delete")
@role_required("admin")
def products_delete(product_id):
    product = db.session.get(Product, product_id) or abort(404)
    used = (SaleItem.query.filter_by(product_id=product.id).first()
            or PurchaseItem.query.filter_by(product_id=product.id).first())
    if used:
        flash("Ce produit figure dans des ventes ou achats : archivez-le plutôt que de le supprimer.", "warning")
        return redirect(url_for("products.detail", product_id=product.id))
    StockMovement.query.filter_by(product_id=product.id).delete()
    audit("suppression_produit", f"{product.sku} - {product.name}")
    db.session.delete(product)
    db.session.commit()
    flash("Produit supprimé.", "success")
    return redirect(url_for("products.index"))


# ---------------------------------------------------------------- Catégories
@products_bp.route("/categories", methods=["GET", "POST"])
@role_required("gestionnaire")
def categories():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Le nom de la catégorie est obligatoire.", "danger")
        elif Category.query.filter(db.func.lower(Category.name) == name.lower()).first():
            flash("Cette catégorie existe déjà.", "danger")
        else:
            db.session.add(Category(name=name[:80]))
            db.session.commit()
            flash("Catégorie ajoutée.", "success")
        return redirect(url_for("products.categories"))
    rows = Category.query.order_by(Category.name).all()
    return render_template("products/categories.html", categories=rows)


@products_bp.route("/categories/<int:category_id>/supprimer", methods=["POST"])
@role_required("gestionnaire")
def delete_category(category_id):
    category = db.session.get(Category, category_id) or abort(404)
    if category.products:
        flash("Impossible : des produits utilisent cette catégorie.", "danger")
    else:
        db.session.delete(category)
        db.session.commit()
        flash("Catégorie supprimée.", "success")
    return redirect(url_for("products.categories"))


@products_bp.route("/api/recherche")
@login_required
def api_search():
    """Recherche rapide utilisée par le formulaire de vente et d'achat."""
    q = request.args.get("q", "").strip()
    query = Product.query.filter_by(is_active=True)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Product.name.ilike(like), Product.sku.ilike(like),
                                    Product.barcode == q))
    return {"results": [
        {"id": p.id, "sku": p.sku, "name": p.name, "price": p.sale_price,
         "cost": p.purchase_price, "stock": p.quantity, "unit": p.unit}
        for p in query.order_by(Product.name).limit(20)
    ]}


# ==================================================================================================
# ÉTAPE 6 — STOCKS
# ==================================================================================================

stock_bp = Blueprint("stock", __name__, url_prefix="/stock")


@stock_bp.route("/", endpoint="index")
@login_required
def stock_index():
    movement_type = request.args.get("type", "")
    product_id = to_int(request.args.get("produit"))
    days = to_int(request.args.get("jours"), 30) or 30
    page = to_int(request.args.get("page"), 1) or 1

    query = StockMovement.query.filter(StockMovement.created_at >= utcnow() - timedelta(days=days))
    if movement_type in MOVEMENT_TYPES:
        query = query.filter_by(movement_type=movement_type)
    if product_id:
        query = query.filter_by(product_id=product_id)
    pagination = query.order_by(StockMovement.created_at.desc()).paginate(page=page, per_page=30, error_out=False)

    products = Product.query.filter_by(is_active=True).order_by(Product.name).all()
    alerts = [p for p in products if p.quantity <= p.min_stock]
    return render_template("stock/index.html", pagination=pagination, movements=pagination.items,
                           products=products, alerts=alerts, types=MOVEMENT_TYPES,
                           movement_type=movement_type, product_id=product_id, days=days,
                           total_value=sum(p.stock_value for p in products))


@stock_bp.route("/mouvement", methods=["GET", "POST"])
@role_required("gestionnaire")
def movement():
    products = Product.query.filter_by(is_active=True).order_by(Product.name).all()
    if request.method == "POST":
        product = db.session.get(Product, to_int(request.form.get("product_id")))
        kind = request.form.get("movement_type")
        qty = to_float(request.form.get("quantity"), -1)
        reason = request.form.get("reason", "").strip()
        errors = []
        if not product:
            errors.append("Choisissez un produit.")
        if kind not in ("entree", "sortie", "ajustement"):
            errors.append("Type de mouvement invalide.")
        if qty < 0 or (kind != "ajustement" and qty == 0):
            errors.append("Quantité invalide.")
        if not reason:
            errors.append("Indiquez le motif du mouvement.")
        if product and kind == "sortie" and qty > product.quantity:
            errors.append(f"Stock insuffisant : {product.quantity:g} {product.unit} disponible(s).")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("stock/movement.html", products=products, form=request.form)

        if kind == "entree":
            delta = qty
        elif kind == "sortie":
            delta = -qty
        else:  # ajustement : la quantité saisie est le stock réel compté
            delta = qty - product.quantity
        if delta == 0:
            flash("Aucun changement : le stock est déjà à cette valeur.", "info")
            return redirect(url_for("stock.index"))
        move_stock(product, delta, kind, reason)
        audit("mouvement_stock", f"{product.sku} {kind} {delta:+g}")
        db.session.commit()
        flash(f"Stock de « {product.name} » mis à jour : {product.quantity:g} {product.unit}.", "success")
        return redirect(url_for("stock.index"))

    return render_template("stock/movement.html", products=products,
                           form={"product_id": request.args.get("produit", "")})


@stock_bp.route("/inventaire", methods=["GET", "POST"])
@role_required("gestionnaire")
def inventory():
    products = Product.query.filter_by(is_active=True).order_by(Product.name).all()
    if request.method == "POST":
        changed = 0
        for product in products:
            raw = request.form.get(f"count_{product.id}", "").strip()
            if raw == "":
                continue
            counted = to_float(raw, -1)
            if counted < 0:
                flash(f"Quantité invalide pour {product.name}.", "danger")
                db.session.rollback()
                return render_template("stock/inventory.html", products=products)
            delta = counted - product.quantity
            if delta != 0:
                move_stock(product, delta, "ajustement", "Inventaire physique",
                           f"INV-{utcnow():%Y%m%d}")
                changed += 1
        audit("inventaire", f"{changed} produit(s) ajusté(s)")
        db.session.commit()
        flash(f"Inventaire enregistré : {changed} produit(s) ajusté(s).", "success")
        return redirect(url_for("stock.index"))
    return render_template("stock/inventory.html", products=products)


@stock_bp.route("/export", endpoint="export")
@login_required
def stock_export():
    rows = [(m.created_at.strftime("%d/%m/%Y %H:%M"), m.product.sku, m.product.name, m.type_label,
             m.quantity, m.stock_after, m.reason, m.reference, m.user.full_name if m.user else "")
            for m in StockMovement.query.order_by(StockMovement.created_at.desc()).limit(5000)]
    return csv_response("mouvements_stock.csv", ["Date", "Code", "Produit", "Type", "Quantité",
                                                 "Stock après", "Motif", "Référence", "Utilisateur"], rows)


# ==================================================================================================
# ÉTAPE 7 — ACHATS ET FOURNISSEURS
# ==================================================================================================

purchases_bp = Blueprint("purchases", __name__, url_prefix="/achats")


# ---------------------------------------------------------------- Fournisseurs
@purchases_bp.route("/fournisseurs")
@login_required
def suppliers():
    q = request.args.get("q", "").strip()
    query = Supplier.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Supplier.name.ilike(like), Supplier.phone.ilike(like),
                                    Supplier.contact.ilike(like)))
    return render_template("purchases/suppliers.html", suppliers=query.order_by(Supplier.name).all(), q=q)


@purchases_bp.route("/fournisseurs/nouveau", methods=["GET", "POST"])
@purchases_bp.route("/fournisseurs/<int:supplier_id>/modifier", methods=["GET", "POST"])
@role_required("gestionnaire")
def supplier_form(supplier_id=None):
    supplier = db.session.get(Supplier, supplier_id) if supplier_id else None
    if supplier_id and not supplier:
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Le nom du fournisseur est obligatoire.", "danger")
            return render_template("purchases/supplier_form.html", supplier=supplier, form=request.form)
        if supplier is None:
            supplier = Supplier()
            db.session.add(supplier)
        supplier.name = name[:150]
        for field, size in (("contact", 120), ("phone", 30), ("email", 120), ("address", 255)):
            setattr(supplier, field, request.form.get(field, "").strip()[:size])
        supplier.notes = request.form.get("notes", "").strip()
        audit("fournisseur", name)
        db.session.commit()
        flash("Fournisseur enregistré.", "success")
        return redirect(url_for("purchases.supplier_detail", supplier_id=supplier.id))
    return render_template("purchases/supplier_form.html", supplier=supplier, form={})


@purchases_bp.route("/fournisseurs/<int:supplier_id>")
@login_required
def supplier_detail(supplier_id):
    supplier = db.session.get(Supplier, supplier_id) or abort(404)
    purchases = Purchase.query.filter_by(supplier_id=supplier.id).order_by(Purchase.created_at.desc()).all()
    valid = [p for p in purchases if p.status != "annule"]
    return render_template("purchases/supplier_detail.html", supplier=supplier, purchases=purchases,
                           total=sum(p.total for p in valid), debt=sum(p.balance for p in valid))


# ---------------------------------------------------------------- Bons d'achat
@purchases_bp.route("/", endpoint="index")
@login_required
def purchases_index():
    status = request.args.get("statut", "")
    page = to_int(request.args.get("page"), 1) or 1
    query = Purchase.query
    if status in PURCHASE_STATUS:
        query = query.filter_by(status=status)
    pagination = query.order_by(Purchase.created_at.desc()).paginate(page=page, per_page=25, error_out=False)
    return render_template("purchases/index.html", pagination=pagination, purchases=pagination.items,
                           status=status, statuses=PURCHASE_STATUS)


def _read_lines(form):
    """Lit les lignes produit envoyées par le formulaire dynamique."""
    lines, errors = [], []
    ids = form.getlist("product_id[]")
    quantities = form.getlist("quantity[]")
    prices = form.getlist("price[]")
    for index, raw_id in enumerate(ids):
        product = db.session.get(Product, to_int(raw_id)) if raw_id else None
        if not product:
            continue
        qty = to_float(quantities[index] if index < len(quantities) else 0)
        price = to_float(prices[index] if index < len(prices) else 0, -1)
        if qty <= 0:
            errors.append(f"Quantité invalide pour {product.name}.")
        if price < 0:
            errors.append(f"Prix invalide pour {product.name}.")
        lines.append((product, qty, price))
    if not lines:
        errors.append("Ajoutez au moins un produit.")
    return lines, errors


@purchases_bp.route("/nouveau", methods=["GET", "POST"], endpoint="create")
@role_required("gestionnaire")
def purchases_create():
    suppliers = Supplier.query.order_by(Supplier.name).all()
    if request.method == "POST":
        supplier = db.session.get(Supplier, to_int(request.form.get("supplier_id")))
        lines, errors = _read_lines(request.form)
        amount_paid = to_float(request.form.get("amount_paid"), 0)
        if not supplier:
            errors.insert(0, "Choisissez un fournisseur.")
        if amount_paid < 0:
            errors.append("Le montant payé ne peut pas être négatif.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("purchases/form.html", suppliers=suppliers, form=request.form)

        purchase = Purchase(reference=next_reference(Purchase, "reference", get_setting("purchase_prefix") or "ACH"),
                            supplier=supplier, user_id=current_user.id,
                            notes=request.form.get("notes", "").strip())
        for product, qty, price in lines:
            purchase.items.append(PurchaseItem(product=product, quantity=qty, unit_cost=price))
        purchase.total = round(sum(i.line_total for i in purchase.items), 2)
        purchase.amount_paid = min(amount_paid, purchase.total)
        db.session.add(purchase)
        if request.form.get("receive_now"):
            _receive(purchase, request.form.get("update_cost") == "1")
        audit("creation_achat", purchase.reference)
        db.session.commit()
        flash(f"Bon d'achat {purchase.reference} enregistré.", "success")
        return redirect(url_for("purchases.detail", purchase_id=purchase.id))
    return render_template("purchases/form.html", suppliers=suppliers, form={})


@purchases_bp.route("/<int:purchase_id>", endpoint="detail")
@login_required
def purchases_detail(purchase_id):
    purchase = db.session.get(Purchase, purchase_id) or abort(404)
    return render_template("purchases/detail.html", purchase=purchase)


def _receive(purchase, update_cost):
    for item in purchase.items:
        move_stock(item.product, item.quantity, "achat", f"Réception {purchase.supplier.name}",
                   purchase.reference)
        if update_cost and item.unit_cost > 0:
            item.product.purchase_price = item.unit_cost
    purchase.status = "recu"
    purchase.received_at = utcnow()


@purchases_bp.route("/<int:purchase_id>/recevoir", methods=["POST"])
@role_required("gestionnaire")
def receive(purchase_id):
    purchase = db.session.get(Purchase, purchase_id) or abort(404)
    if purchase.status != "commande":
        flash("Ce bon n'est plus en attente de réception.", "warning")
    else:
        _receive(purchase, request.form.get("update_cost") == "1")
        audit("reception_achat", purchase.reference)
        db.session.commit()
        flash("Marchandises reçues : le stock a été mis à jour.", "success")
    return redirect(url_for("purchases.detail", purchase_id=purchase.id))


@purchases_bp.route("/<int:purchase_id>/annuler", methods=["POST"], endpoint="cancel")
@role_required("gestionnaire")
def purchases_cancel(purchase_id):
    purchase = db.session.get(Purchase, purchase_id) or abort(404)
    if purchase.status != "commande":
        flash("Seul un bon non reçu peut être annulé.", "warning")
    else:
        purchase.status = "annule"
        audit("annulation_achat", purchase.reference)
        db.session.commit()
        flash("Bon d'achat annulé.", "success")
    return redirect(url_for("purchases.detail", purchase_id=purchase.id))


@purchases_bp.route("/<int:purchase_id>/paiement", methods=["POST"], endpoint="payment")
@role_required("gestionnaire")
def purchases_payment(purchase_id):
    purchase = db.session.get(Purchase, purchase_id) or abort(404)
    amount = to_float(request.form.get("amount"), 0)
    if purchase.status == "annule" or amount <= 0 or amount > purchase.balance + 0.01:
        flash("Montant de paiement invalide.", "danger")
    else:
        purchase.amount_paid = round(purchase.amount_paid + amount, 2)
        audit("paiement_fournisseur", f"{purchase.reference} {amount:g}")
        db.session.commit()
        flash("Paiement fournisseur enregistré.", "success")
    return redirect(url_for("purchases.detail", purchase_id=purchase.id))


@purchases_bp.route("/export", endpoint="export")
@login_required
def purchases_export():
    rows = [(p.reference, p.created_at.strftime("%d/%m/%Y"), p.supplier.name, p.status_label,
             p.total, p.amount_paid, p.balance)
            for p in Purchase.query.order_by(Purchase.created_at.desc())]
    return csv_response("achats.csv", ["Référence", "Date", "Fournisseur", "Statut", "Total",
                                       "Payé", "Reste"], rows)


# ==================================================================================================
# ÉTAPE 8 — VENTES ET FACTURATION
# ==================================================================================================

sales_bp = Blueprint("sales", __name__, url_prefix="/ventes")


def _parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _filtered_query():
    q = request.args.get("q", "").strip()
    status = request.args.get("statut", "")
    start = _parse_date(request.args.get("du"))
    end = _parse_date(request.args.get("au"))
    query = Sale.query
    if q:
        like = f"%{q}%"
        query = query.outerjoin(Customer).filter(db.or_(Sale.invoice_number.ilike(like),
                                                        Customer.name.ilike(like),
                                                        Customer.phone.ilike(like)))
    if status in SALE_STATUS:
        query = query.filter(Sale.status == status)
    if start:
        query = query.filter(Sale.created_at >= start)
    if end:
        query = query.filter(Sale.created_at < end + timedelta(days=1))
    return query


@sales_bp.route("/", endpoint="index")
@login_required
def sales_index():
    page = to_int(request.args.get("page"), 1) or 1
    query = _filtered_query()
    valid = query.filter(Sale.status != "annulee")
    total = valid.with_entities(db.func.coalesce(db.func.sum(Sale.total), 0)).scalar()
    pagination = query.order_by(Sale.created_at.desc()).paginate(page=page, per_page=25, error_out=False)
    return render_template("sales/index.html", pagination=pagination, sales=pagination.items,
                           statuses=SALE_STATUS, total=total or 0, args=request.args)


@sales_bp.route("/export", endpoint="export")
@login_required
def sales_export():
    rows = [(s.invoice_number, s.created_at.strftime("%d/%m/%Y %H:%M"),
             s.customer.name if s.customer else "Client comptant", s.status_label, s.payment_label,
             s.subtotal, s.discount, s.tax_amount, s.total, s.amount_paid, s.balance,
             s.user.full_name if s.user else "")
            for s in _filtered_query().order_by(Sale.created_at.desc()).limit(10000)]
    return csv_response("ventes.csv", ["Facture", "Date", "Client", "Statut", "Paiement", "Sous-total",
                                       "Remise", "Taxe", "Total", "Payé", "Reste", "Vendeur"], rows)


@sales_bp.route("/nouvelle", methods=["GET", "POST"], endpoint="create")
@role_required("gestionnaire", "vendeur")
def sales_create():
    customers = Customer.query.order_by(Customer.name).all()
    tax_rate = to_float(get_setting("tax_rate"), 0)
    if request.method == "POST":
        f = request.form
        errors, lines = [], []
        requested = {}
        ids = f.getlist("product_id[]")
        quantities = f.getlist("quantity[]")
        prices = f.getlist("price[]")
        for index, raw_id in enumerate(ids):
            product = db.session.get(Product, to_int(raw_id)) if raw_id else None
            if not product or not product.is_active:
                continue
            qty = to_float(quantities[index] if index < len(quantities) else 0)
            price = to_float(prices[index] if index < len(prices) else 0, -1)
            if qty <= 0:
                errors.append(f"Quantité invalide pour {product.name}.")
                continue
            if price < 0:
                errors.append(f"Prix invalide pour {product.name}.")
                continue
            requested[product.id] = requested.get(product.id, 0) + qty
            lines.append((product, qty, price))
        if not lines and not errors:
            errors.append("Ajoutez au moins un produit à la vente.")
        for product_id, qty in requested.items():
            product = db.session.get(Product, product_id)
            if qty > product.quantity:
                errors.append(f"Stock insuffisant pour {product.name} : {product.quantity:g} disponible(s).")

        customer = None
        new_customer_name = f.get("new_customer_name", "").strip()
        if new_customer_name:
            customer = Customer(name=new_customer_name[:150], phone=f.get("new_customer_phone", "").strip()[:30])
            db.session.add(customer)
        elif f.get("customer_id"):
            customer = db.session.get(Customer, to_int(f.get("customer_id")))

        payment_method = f.get("payment_method", "especes")
        if payment_method not in PAYMENT_METHODS:
            errors.append("Mode de paiement invalide.")
        discount = to_float(f.get("discount"), 0)
        subtotal = round(sum(q * p for _, q, p in lines), 2)
        if discount < 0 or discount > subtotal:
            errors.append("La remise doit être comprise entre 0 et le sous-total.")
        tax_amount = round((subtotal - discount) * tax_rate / 100, 2)
        total = round(subtotal - discount + tax_amount, 2)
        paid_raw = f.get("amount_paid", "").strip()
        amount_paid = total if paid_raw == "" else to_float(paid_raw, -1)
        if amount_paid < 0:
            errors.append("Montant payé invalide.")
        if amount_paid < total and not customer:
            errors.append("Une vente à crédit ou partielle nécessite un client identifié.")

        if errors:
            db.session.rollback()
            for e in errors:
                flash(e, "danger")
            return render_template("sales/form.html", customers=customers, tax_rate=tax_rate,
                                   payment_methods=PAYMENT_METHODS, form=f)

        change = max(amount_paid - total, 0)
        amount_paid = min(amount_paid, total)
        sale = Sale(invoice_number=next_reference(Sale, "invoice_number", get_setting("invoice_prefix") or "FAC"),
                    customer=customer, subtotal=subtotal, discount=discount, tax_rate=tax_rate,
                    tax_amount=tax_amount, total=total, amount_paid=amount_paid,
                    payment_method=payment_method, user_id=current_user.id,
                    notes=f.get("notes", "").strip())
        sale.status = "payee" if amount_paid >= total else ("impayee" if amount_paid == 0 else "partielle")
        for product, qty, price in lines:
            sale.items.append(SaleItem(product=product, quantity=qty, unit_price=price,
                                       unit_cost=product.purchase_price))
            move_stock(product, -qty, "vente", "Vente", sale.invoice_number)
        db.session.add(sale)
        audit("vente", f"{sale.invoice_number} total={total:g}")
        db.session.commit()
        message = f"Vente {sale.invoice_number} enregistrée."
        if change > 0:
            message += f" Monnaie à rendre : {change:,.0f}".replace(",", " ") + f" {get_setting('currency')}."
        flash(message, "success")
        return redirect(url_for("sales.detail", sale_id=sale.id))

    return render_template("sales/form.html", customers=customers, tax_rate=tax_rate,
                           payment_methods=PAYMENT_METHODS, form={})


@sales_bp.route("/<int:sale_id>", endpoint="detail")
@login_required
def sales_detail(sale_id):
    sale = db.session.get(Sale, sale_id) or abort(404)
    return render_template("sales/detail.html", sale=sale)


@sales_bp.route("/<int:sale_id>/facture")
@login_required
def invoice(sale_id):
    sale = db.session.get(Sale, sale_id) or abort(404)
    return render_template("sales/invoice.html", sale=sale, ticket=request.args.get("format") == "ticket")


@sales_bp.route("/<int:sale_id>/paiement", methods=["POST"], endpoint="payment")
@role_required("gestionnaire", "vendeur")
def sales_payment(sale_id):
    sale = db.session.get(Sale, sale_id) or abort(404)
    amount = to_float(request.form.get("amount"), 0)
    if sale.status == "annulee" or amount <= 0 or amount > sale.balance + 0.01:
        flash("Montant invalide.", "danger")
    else:
        sale.amount_paid = round(sale.amount_paid + amount, 2)
        sale.status = "payee" if sale.amount_paid >= sale.total - 0.01 else "partielle"
        audit("paiement_client", f"{sale.invoice_number} {amount:g}")
        db.session.commit()
        flash("Paiement enregistré.", "success")
    return redirect(url_for("sales.detail", sale_id=sale.id))


@sales_bp.route("/<int:sale_id>/annuler", methods=["POST"], endpoint="cancel")
@role_required("gestionnaire")
def sales_cancel(sale_id):
    sale = db.session.get(Sale, sale_id) or abort(404)
    reason = request.form.get("reason", "").strip()
    if sale.status == "annulee":
        flash("Cette vente est déjà annulée.", "warning")
    elif not reason:
        flash("Indiquez le motif de l'annulation.", "danger")
    else:
        for item in sale.items:
            move_stock(item.product, item.quantity, "retour", f"Annulation : {reason}", sale.invoice_number)
        sale.status = "annulee"
        sale.notes = ((sale.notes or "") + f"\nAnnulée le {utcnow():%d/%m/%Y} : {reason}").strip()
        audit("annulation_vente", f"{sale.invoice_number} : {reason}")
        db.session.commit()
        flash("Vente annulée : les produits ont été remis en stock.", "success")
    return redirect(url_for("sales.detail", sale_id=sale.id))


# ---------------------------------------------------------------- Clients
@sales_bp.route("/clients")
@login_required
def customers():
    q = request.args.get("q", "").strip()
    query = Customer.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Customer.name.ilike(like), Customer.phone.ilike(like)))
    return render_template("sales/customers.html", customers=query.order_by(Customer.name).all(), q=q)


@sales_bp.route("/clients/nouveau", methods=["GET", "POST"])
@sales_bp.route("/clients/<int:customer_id>/modifier", methods=["GET", "POST"])
@login_required
def customer_form(customer_id=None):
    customer = db.session.get(Customer, customer_id) if customer_id else None
    if customer_id and not customer:
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Le nom du client est obligatoire.", "danger")
            return render_template("sales/customer_form.html", customer=customer, form=request.form)
        if customer is None:
            customer = Customer()
            db.session.add(customer)
        customer.name = name[:150]
        customer.phone = request.form.get("phone", "").strip()[:30]
        customer.email = request.form.get("email", "").strip()[:120]
        customer.address = request.form.get("address", "").strip()[:255]
        db.session.commit()
        flash("Client enregistré.", "success")
        return redirect(url_for("sales.customer_detail", customer_id=customer.id))
    return render_template("sales/customer_form.html", customer=customer, form={})


@sales_bp.route("/clients/<int:customer_id>")
@login_required
def customer_detail(customer_id):
    customer = db.session.get(Customer, customer_id) or abort(404)
    sales = Sale.query.filter_by(customer_id=customer.id).order_by(Sale.created_at.desc()).all()
    valid = [s for s in sales if s.status != "annulee"]
    return render_template("sales/customer_detail.html", customer=customer, sales=sales,
                           total=sum(s.total for s in valid), debt=sum(s.balance for s in valid))


# ==================================================================================================
# ÉTAPE 9 — ANALYSES ET RECOMMANDATIONS
# ==================================================================================================

analytics_bp = Blueprint("analytics", __name__, url_prefix="/analyses")

DAYS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
MONTHS_FR = ["Janv", "Févr", "Mars", "Avr", "Mai", "Juin", "Juil", "Août", "Sept", "Oct", "Nov", "Déc"]


def build_analysis(days):
    now = utcnow()
    start = now - timedelta(days=days)
    sales = Sale.query.filter(Sale.status != "annulee", Sale.created_at >= start).all()
    products = Product.query.filter_by(is_active=True).all()

    revenue = sum(s.total for s in sales)
    profit = sum(s.profit for s in sales)
    summary = {
        "revenue": revenue,
        "profit": profit,
        "margin": (profit / revenue * 100) if revenue else 0,
        "count": len(sales),
        "basket": revenue / len(sales) if sales else 0,
        "receivables": sum(s.balance for s in Sale.query.filter(Sale.status.in_(["partielle", "impayee"]))),
    }

    per_product = defaultdict(lambda: {"qty": 0, "revenue": 0, "profit": 0, "last": None})
    by_category = defaultdict(float)
    by_payment = defaultdict(float)
    by_weekday = [0.0] * 7
    for sale in sales:
        by_payment[PAYMENT_METHODS.get(sale.payment_method, sale.payment_method)] += sale.total
        by_weekday[sale.created_at.weekday()] += sale.total
        for item in sale.items:
            row = per_product[item.product_id]
            row["qty"] += item.quantity
            row["revenue"] += item.line_total
            row["profit"] += item.quantity * (item.unit_price - item.unit_cost)
            if row["last"] is None or sale.created_at > row["last"]:
                row["last"] = sale.created_at
            category = item.product.category.name if item.product.category else "Sans catégorie"
            by_category[category] += item.line_total

    product_map = {p.id: p for p in Product.query.all()}
    top = sorted(per_product.items(), key=lambda kv: kv[1]["revenue"], reverse=True)[:10]
    top_products = [{"product": product_map[pid], **data} for pid, data in top if pid in product_map]

    # 12 derniers mois
    months = []
    cursor = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for _ in range(12):
        months.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    months.reverse()
    year_sales = Sale.query.filter(Sale.status != "annulee", Sale.created_at >= months[0]).all()
    monthly_rev = defaultdict(float)
    monthly_profit = defaultdict(float)
    for sale in year_sales:
        key = (sale.created_at.year, sale.created_at.month)
        monthly_rev[key] += sale.total
        monthly_profit[key] += sale.profit
    chart = {
        "labels": [f"{MONTHS_FR[m.month - 1]} {str(m.year)[2:]}" for m in months],
        "revenue": [round(monthly_rev[(m.year, m.month)]) for m in months],
        "profit": [round(monthly_profit[(m.year, m.month)]) for m in months],
        "categories": {k: round(v) for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])},
        "payments": {k: round(v) for k, v in sorted(by_payment.items(), key=lambda kv: -kv[1])},
        "weekdays": {"labels": DAYS_FR, "data": [round(v) for v in by_weekday]},
    }

    recommendations = _recommendations(products, per_product, days, summary, by_weekday)
    return summary, top_products, chart, recommendations


def _recommendations(products, per_product, days, summary, by_weekday):
    reorder, dormant, low_margin = [], [], []
    for product in products:
        sold = per_product.get(product.id, {}).get("qty", 0)
        daily = sold / days if days else 0
        cover = product.quantity / daily if daily > 0 else None
        if product.quantity <= product.min_stock or (cover is not None and cover < 14):
            target = max(daily * 30, product.min_stock * 2)
            qty = math.ceil(max(target - product.quantity, 0))
            if qty > 0:
                reorder.append({"product": product, "daily": daily, "cover": cover, "qty": qty,
                                "cost": qty * product.purchase_price,
                                "urgent": product.quantity <= 0 or (cover is not None and cover < 7)})
        if product.quantity > 0 and sold == 0:
            dormant.append(product)
        if product.sale_price > 0 and product.margin_rate < 10:
            low_margin.append(product)

    reorder.sort(key=lambda r: (not r["urgent"], r["cover"] if r["cover"] is not None else 0))
    dormant.sort(key=lambda p: -p.stock_value)

    advice = []
    urgent = [r for r in reorder if r["urgent"]]
    if urgent:
        advice.append(("danger", f"{len(urgent)} produit(s) en rupture ou à moins de 7 jours de stock : "
                                 "passez commande en priorité."))
    if dormant:
        value = sum(p.stock_value for p in dormant)
        advice.append(("warning", f"{len(dormant)} produit(s) sans vente sur la période immobilisent "
                                  f"{money(value)} : envisagez une promotion ou un déstockage."))
    if low_margin:
        advice.append(("warning", f"{len(low_margin)} produit(s) ont une marge inférieure à 10 % : "
                                  "révisez leur prix de vente ou négociez le prix d'achat."))
    if summary["receivables"] > 0:
        advice.append(("info", f"{money(summary['receivables'])} restent à encaisser auprès des clients : "
                               "relancez les factures impayées."))
    if any(by_weekday):
        best = max(range(7), key=lambda i: by_weekday[i])
        advice.append(("success", f"Le {DAYS_FR[best].lower()} est votre meilleur jour de vente : "
                                  "renforcez le personnel et le stock ce jour-là."))
    if summary["count"] and summary["margin"] < 15:
        advice.append(("warning", f"Marge moyenne de {summary['margin']:.1f} % : surveillez vos remises."))
    if not advice:
        advice.append(("success", "Aucune alerte : votre activité est saine sur cette période."))
    return {"reorder": reorder, "dormant": dormant[:15], "low_margin": low_margin[:15], "advice": advice}


def _period():
    days = to_int(request.args.get("jours"), 30)
    return days if days in (7, 30, 90, 180, 365) else 30


@analytics_bp.route("/", endpoint="index")
@role_required("gestionnaire")
def analytics_index():
    days = _period()
    summary, top_products, chart, recommendations = build_analysis(days)
    return render_template("analytics/index.html", days=days, summary=summary, top_products=top_products,
                           chart=chart, rec=recommendations)


@analytics_bp.route("/commande-suggeree.csv")
@role_required("gestionnaire")
def export_reorder():
    _, _, _, rec = build_analysis(_period())
    rows = [(r["product"].sku, r["product"].name, r["product"].quantity, round(r["daily"], 2),
             round(r["cover"]) if r["cover"] is not None else "", r["qty"], r["cost"],
             "Oui" if r["urgent"] else "Non") for r in rec["reorder"]]
    return csv_response("commande_suggeree.csv", ["Code", "Produit", "Stock", "Ventes/jour",
                                                  "Jours de stock", "Qté à commander", "Coût estimé",
                                                  "Urgent"], rows)


# ==================================================================================================
# ÉTAPE 10 — ARCHIVES, DOCUMENTS ET MESSAGES
# ==================================================================================================

archives_bp = Blueprint("archives", __name__, url_prefix="/archives")


def _allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in current_app.config["ALLOWED_EXTENSIONS"]


# ---------------------------------------------------------------- Documents
@archives_bp.route("/")
@login_required
def documents():
    q = request.args.get("q", "").strip()
    category = request.args.get("categorie", "")
    query = Document.query.with_entities(
        Document.id, Document.title, Document.category, Document.original_name, Document.size,
        Document.created_at, Document.uploaded_by)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Document.title.ilike(like), Document.original_name.ilike(like),
                                    Document.description.ilike(like)))
    if category in DOC_CATEGORIES:
        query = query.filter(Document.category == category)
    rows = query.order_by(Document.created_at.desc()).limit(300).all()
    users = {u.id: u.full_name for u in User.query.all()}
    return render_template("archives/documents.html", documents=rows, users=users,
                           categories=DOC_CATEGORIES, q=q, category=category)


@archives_bp.route("/ajouter", methods=["POST"])
@login_required
def upload():
    file = request.files.get("file")
    title = request.form.get("title", "").strip()
    category = request.form.get("category", "autre")
    if not file or not file.filename:
        flash("Choisissez un fichier.", "danger")
        return redirect(url_for("archives.documents"))
    filename = secure_filename(file.filename) or "document"
    if not _allowed(file.filename):
        flash("Type de fichier non autorisé (PDF, images, Word, Excel, CSV, texte).", "danger")
        return redirect(url_for("archives.documents"))
    data = file.read()
    if len(data) > current_app.config["MAX_DOCUMENT_SIZE"]:
        flash("Fichier trop volumineux (5 Mo maximum).", "danger")
        return redirect(url_for("archives.documents"))
    if not data:
        flash("Le fichier est vide.", "danger")
        return redirect(url_for("archives.documents"))
    doc = Document(title=(title or file.filename)[:200],
                   category=category if category in DOC_CATEGORIES else "autre",
                   description=request.form.get("description", "").strip(),
                   original_name=file.filename[:255],
                   mimetype=mimetypes.guess_type(filename)[0] or "application/octet-stream",
                   size=len(data), data=data, uploaded_by=current_user.id)
    db.session.add(doc)
    audit("ajout_document", doc.title)
    db.session.commit()
    flash("Document archivé.", "success")
    return redirect(url_for("archives.documents"))


@archives_bp.route("/<int:doc_id>/telecharger")
@login_required
def download(doc_id):
    doc = db.session.get(Document, doc_id) or abort(404)
    inline = request.args.get("voir") == "1" and (doc.mimetype or "").startswith(("image/", "application/pdf"))
    return send_file(io.BytesIO(doc.data), mimetype=doc.mimetype, as_attachment=not inline,
                     download_name=secure_filename(doc.original_name) or "document")


@archives_bp.route("/<int:doc_id>/supprimer", methods=["POST"], endpoint="delete")
@login_required
def archives_delete(doc_id):
    doc = db.session.get(Document, doc_id) or abort(404)
    if current_user.effective_role not in ("admin", "gestionnaire") and doc.uploaded_by != current_user.id:
        abort(403)
    audit("suppression_document", doc.title)
    db.session.delete(doc)
    db.session.commit()
    flash("Document supprimé.", "success")
    return redirect(url_for("archives.documents"))


# ---------------------------------------------------------------- Messagerie interne
def _inbox_query():
    return Message.query.filter(
        db.or_(Message.recipient_id == current_user.id, Message.recipient_id.is_(None)),
        Message.sender_id != current_user.id)


@archives_bp.route("/messages")
@login_required
def messages():
    box = request.args.get("boite", "recus")
    if box == "envoyes":
        rows = Message.query.filter_by(sender_id=current_user.id)
    else:
        rows = _inbox_query()
    rows = rows.order_by(Message.created_at.desc()).limit(200).all()
    return render_template("archives/messages.html", messages=rows, box=box)


@archives_bp.route("/messages/nouveau", methods=["GET", "POST"])
@login_required
def compose():
    users = User.query.filter(User.id != current_user.id, User.is_active_account.is_(True)) \
        .order_by(User.full_name).all()
    if request.method == "POST":
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "").strip()
        recipient_raw = request.form.get("recipient_id", "")
        recipient_id = None if recipient_raw == "all" else to_int(recipient_raw)
        errors = []
        if not subject or not body:
            errors.append("Le sujet et le message sont obligatoires.")
        if recipient_raw != "all" and not db.session.get(User, recipient_id or 0):
            errors.append("Choisissez un destinataire.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("archives/compose.html", users=users, form=request.form)
        db.session.add(Message(sender_id=current_user.id, recipient_id=recipient_id,
                               subject=subject[:200], body=body))
        db.session.commit()
        flash("Message envoyé.", "success")
        return redirect(url_for("archives.messages", boite="envoyes"))
    form = {}
    reply = db.session.get(Message, to_int(request.args.get("repondre"), 0) or 0)
    if reply and (reply.recipient_id in (None, current_user.id)):
        form = {"recipient_id": str(reply.sender_id), "subject": "RE : " + reply.subject}
    return render_template("archives/compose.html", users=users, form=form)


@archives_bp.route("/messages/<int:message_id>")
@login_required
def message_detail(message_id):
    message = db.session.get(Message, message_id) or abort(404)
    if message.sender_id != current_user.id and message.recipient_id not in (None, current_user.id):
        abort(403)
    if message.recipient_id == current_user.id and not message.is_read:
        message.is_read = True
        db.session.commit()
    return render_template("archives/message_detail.html", message=message)


@archives_bp.route("/messages/tout-lu", methods=["POST"])
@login_required
def mark_all_read():
    for message in _inbox_query().filter(Message.is_read.is_(False)):
        if message.recipient_id == current_user.id:
            message.is_read = True
    db.session.commit()
    return redirect(url_for("archives.messages"))


@archives_bp.route("/messages/<int:message_id>/supprimer", methods=["POST"])
@role_required("admin")
def delete_message(message_id):
    message = db.session.get(Message, message_id) or abort(404)
    db.session.delete(message)
    db.session.commit()
    flash("Message supprimé.", "success")
    return redirect(url_for("archives.messages"))


# ==================================================================================================
# ÉTAPE 11 — PARAMÈTRES ET SÉCURITÉ
# ==================================================================================================

settings_bp = Blueprint("settings", __name__, url_prefix="/parametres")

CURRENCIES = ["GNF", "FCFA", "EUR", "USD"]
LOGO_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
MAX_LOGO_SIZE = 1024 * 1024  # 1 Mo
COMPANY_FIELD_SIZES = {"name": 150, "activity": 150, "address": 255, "city": 80, "country": 80,
                       "phone": 60, "email": 120, "website": 150, "rccm": 80, "nif": 80}
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _image_type(data):
    """Vérifie le contenu réel du fichier (et pas seulement son extension)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _normalize_logo(data):
    """Vérifie l'image et la convertit en PNG de 600 px maximum (léger et lisible partout)."""
    from PIL import Image as PILImage
    try:
        image = PILImage.open(io.BytesIO(data))
        image.load()
        image = image.convert("RGBA")
        image.thumbnail((600, 600))
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:
        return None


@settings_bp.route("/", methods=["GET", "POST"], endpoint="index")
@role_required("admin")
def settings_index():
    company = db.session.get(Company, g.company_id) or abort(404)
    if request.method == "POST":
        f = request.form
        errors = []
        tax = to_float(f.get("tax_rate"), -1)
        if tax < 0 or tax > 100:
            errors.append("Le taux de taxe doit être entre 0 et 100.")
        if not f.get("name", "").strip():
            errors.append("Le nom de l'entreprise est obligatoire.")
        if f.get("currency") not in CURRENCIES:
            errors.append("Devise invalide.")
        for key in ("invoice_prefix", "purchase_prefix"):
            value = f.get(key, "").strip().upper()
            if not value.isalnum() or len(value) > 6:
                errors.append("Les préfixes doivent contenir 1 à 6 lettres ou chiffres.")
                break
        color = f.get("invoice_color", "#0b5d43").strip()
        if not HEX_COLOR.match(color):
            errors.append("Couleur de facture invalide.")

        logo_file = request.files.get("logo")
        logo_data = None
        if logo_file and logo_file.filename:
            ext = logo_file.filename.rsplit(".", 1)[-1].lower() if "." in logo_file.filename else ""
            logo_data = logo_file.read()
            if ext not in LOGO_TYPES or not _image_type(logo_data):
                errors.append("Logo : image PNG, JPG ou WEBP uniquement.")
                logo_data = None
            elif len(logo_data) > MAX_LOGO_SIZE:
                errors.append("Logo trop lourd (1 Mo maximum).")
                logo_data = None
            else:
                logo_data = _normalize_logo(logo_data)
                if logo_data is None:
                    errors.append("Logo illisible : enregistrez-le à nouveau en PNG ou JPG.")

        if errors:
            for e in errors:
                flash(e, "danger")
            values = all_settings()
            values.update({k: v for k, v in f.items() if k != "csrf_token"})
            return render_template("settings/index.html", values=values, company=company,
                                   currencies=CURRENCIES)

        for field in COMPANY_FIELDS:
            setattr(company, field, f.get(field, "").strip()[:COMPANY_FIELD_SIZES[field]])
        if logo_data:
            company.logo = logo_data
            company.logo_mimetype = "image/png"
        if f.get("remove_logo") and not logo_data:
            company.logo = None
            company.logo_mimetype = None

        for key in DEFAULT_SETTINGS:
            if key == "tax_rate":
                set_setting(key, f"{tax:g}")
            elif key in ("invoice_prefix", "purchase_prefix"):
                set_setting(key, f.get(key, "").strip().upper())
            elif key in ("low_stock_alert", "show_logo_on_invoice"):
                set_setting(key, "1" if f.get(key) else "0")
            elif key == "invoice_color":
                set_setting(key, color)
            else:
                set_setting(key, f.get(key, "").strip()[:1000])
        audit("parametres", "Mise à jour des paramètres")
        db.session.commit()
        flash("Paramètres enregistrés.", "success")
        return redirect(url_for("settings.index"))
    return render_template("settings/index.html", values=all_settings(), company=company,
                           currencies=CURRENCIES)


@settings_bp.route("/logo")
def logo():
    """Logo de l'entreprise connectée (utilisé par le menu et les factures)."""
    company = db.session.get(Company, g.get("company_id") or 0)
    if not company or not company.logo:
        abort(404)
    return Response(company.logo, mimetype=company.logo_mimetype or "image/png",
                    headers={"Cache-Control": "private, max-age=300"})


@settings_bp.route("/journal")
@role_required("admin")
def logs():
    page = to_int(request.args.get("page"), 1) or 1
    action = request.args.get("action", "").strip()
    query = AuditLog.query
    if action:
        query = query.filter(AuditLog.action.ilike(f"%{action}%"))
    pagination = query.order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template("settings/logs.html", pagination=pagination, logs=pagination.items, action=action)


def _rows(model, exclude=(), query=None):
    result = []
    for obj in (query if query is not None else model.query).all():
        row = {}
        for column in model.__table__.columns:
            if column.name in exclude:
                continue
            value = getattr(obj, column.name)
            row[column.name] = value.isoformat() if hasattr(value, "isoformat") else value
        result.append(row)
    return result


@settings_bp.route("/sauvegarde")
@role_required("admin")
def backup():
    data = {
        "application": "ANSARTECH 224",
        "date": utcnow().isoformat(),
        "parametres": all_settings(),
        "entreprise": _rows(Company, exclude=("logo",), query=Company.query.filter_by(id=g.company_id)),
        "utilisateurs": _rows(User, exclude=("password_hash",)),
        "categories": _rows(Category),
        "produits": _rows(Product),
        "mouvements_stock": _rows(StockMovement),
        "fournisseurs": _rows(Supplier),
        "achats": _rows(Purchase),
        "lignes_achat": _rows(PurchaseItem, query=PurchaseItem.query.join(Purchase)),
        "clients": _rows(Customer),
        "ventes": _rows(Sale),
        "lignes_vente": _rows(SaleItem, query=SaleItem.query.join(Sale)),
    }
    audit("sauvegarde", "Export JSON complet")
    db.session.commit()
    filename = f"sauvegarde_ansartech224_{utcnow():%Y%m%d_%H%M}.json"
    return Response(json.dumps(data, ensure_ascii=False, indent=1), mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


# ==================================================================================================
# ESPACE PLATEFORME (super-admin, entreprises clientes)
# ==================================================================================================

platform_bp = Blueprint("platform", __name__, url_prefix="/plateforme")

COMPANY_TEXT_FIELDS = {
    "name": 150, "activity": 150, "address": 255, "city": 80, "country": 80, "phone": 60,
    "email": 120, "website": 150, "rccm": 80, "nif": 80, "contact_name": 120,
}


def _password_errors(password):
    return password_errors(password)


def _parse_iso_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return False


def _company_stats(company):
    since = utcnow() - timedelta(days=30)
    sales = Sale.query.filter(Sale.company_id == company.id, Sale.status != "annulee",
                              Sale.created_at >= since)
    revenue = sales.with_entities(db.func.coalesce(db.func.sum(Sale.total), 0)).scalar() or 0
    last = (AuditLog.query.filter(AuditLog.company_id == company.id)
            .order_by(AuditLog.created_at.desc()).first())
    return {
        "users": User.query.filter_by(company_id=company.id).count(),
        "products": Product.query.filter_by(company_id=company.id).count(),
        "sales_30": sales.count(),
        "revenue_30": revenue,
        "last_activity": last.created_at if last else None,
    }


@platform_bp.route("/", endpoint="index")
@superadmin_required
def platform_index():
    q = request.args.get("q", "").strip()
    status = request.args.get("statut", "")
    query = Company.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Company.name.ilike(like), Company.code.ilike(like),
                                    Company.phone.ilike(like), Company.city.ilike(like)))
    companies = query.order_by(Company.name).all()
    today = utcnow().date()
    if status == "actives":
        companies = [c for c in companies if c.can_login]
    elif status == "suspendues":
        companies = [c for c in companies if not c.is_active]
    elif status == "expirees":
        companies = [c for c in companies if c.is_expired]
    elif status == "bientot":
        companies = [c for c in companies if c.subscription_end and today <= c.subscription_end <= today + timedelta(days=15)]
    stats = {c.id: _company_stats(c) for c in companies}
    all_companies = Company.query.all()
    totals = {
        "companies": len(all_companies),
        "active": sum(1 for c in all_companies if c.can_login),
        "expiring": sum(1 for c in all_companies if c.subscription_end
                        and today <= c.subscription_end <= today + timedelta(days=15)),
        "users": User.query.filter(User.company_id.isnot(None)).count(),
    }
    return render_template("platform/index.html", companies=companies, stats=stats, totals=totals,
                           q=q, status=status, today=today)


def _read_company_form(form, company=None):
    errors, values = [], {}
    for field, size in COMPANY_TEXT_FIELDS.items():
        values[field] = form.get(field, "").strip()[:size]
    if not values["name"]:
        errors.append("Le nom de l'entreprise est obligatoire.")
    code = form.get("code", "").strip().lower()
    if code:
        if not re.fullmatch(r"[a-z0-9-]{2,30}", code):
            errors.append("Code : 2 à 30 caractères (lettres minuscules, chiffres, tirets).")
        other = Company.query.filter_by(code=code).first()
        if other and (company is None or other.id != company.id):
            errors.append("Ce code d'entreprise est déjà utilisé.")
        values["code"] = code
    end = _parse_iso_date(form.get("subscription_end", ""))
    if end is False:
        errors.append("Date de fin d'abonnement invalide.")
    values["subscription_end"] = end or None
    max_users = to_int(form.get("max_users"), 0)
    if not max_users or max_users < 1 or max_users > 500:
        errors.append("Nombre d'utilisateurs autorisés : entre 1 et 500.")
    values["max_users"] = max_users or 1
    values["notes"] = form.get("notes", "").strip()
    return values, errors


@platform_bp.route("/entreprises/nouvelle", methods=["GET", "POST"])
@superadmin_required
def company_new():
    if request.method == "POST":
        values, errors = _read_company_form(request.form)
        username = request.form.get("admin_username", "").strip().lower()
        password = request.form.get("admin_password", "")
        admin_name = request.form.get("admin_full_name", "").strip() or "Administrateur"
        if not re.fullmatch(r"[a-z0-9._-]{3,50}", username):
            errors.append("Identifiant de l'administrateur : 3 à 50 caractères (lettres minuscules, chiffres, . _ -).")
        elif User.query.filter(db.func.lower(User.username) == username).first():
            errors.append("Cet identifiant est déjà utilisé sur la plateforme.")
        errors += _password_errors(password)
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("platform/company_form.html", company=None, form=request.form)
        name = values.pop("name")
        company, _admin = create_company(name, username, password, admin_name[:120], **values)
        audit("creation_entreprise", f"{company.name} ({company.code}) admin={username}", company_id=company.id)
        db.session.commit()
        flash(f"Entreprise « {company.name} » créée. Identifiant administrateur : {username} "
              "(mot de passe à changer à la 1re connexion).", "success")
        return redirect(url_for("platform.company_detail", company_id=company.id))
    default_end = (utcnow().date() + timedelta(days=30)).isoformat()
    return render_template("platform/company_form.html", company=None,
                           form={"subscription_end": default_end, "max_users": "5",
                                 "city": "Conakry", "country": "Guinée"})


@platform_bp.route("/entreprises/<int:company_id>")
@superadmin_required
def company_detail(company_id):
    company = db.session.get(Company, company_id) or abort(404)
    users = User.query.filter_by(company_id=company.id).order_by(User.role, User.full_name).all()
    logs = (AuditLog.query.filter_by(company_id=company.id).order_by(AuditLog.created_at.desc()).limit(15).all())
    return render_template("platform/company_detail.html", company=company, users=users,
                           stats=_company_stats(company), logs=logs)


@platform_bp.route("/entreprises/<int:company_id>/modifier", methods=["GET", "POST"])
@superadmin_required
def company_edit(company_id):
    company = db.session.get(Company, company_id) or abort(404)
    if request.method == "POST":
        values, errors = _read_company_form(request.form, company)
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("platform/company_form.html", company=company, form=request.form)
        for key, value in values.items():
            setattr(company, key, value)
        audit("modification_entreprise", company.name, company_id=company.id)
        db.session.commit()
        flash("Entreprise mise à jour.", "success")
        return redirect(url_for("platform.company_detail", company_id=company.id))
    form = {field: getattr(company, field) or "" for field in COMPANY_TEXT_FIELDS}
    form.update(code=company.code, max_users=company.max_users, notes=company.notes or "",
                subscription_end=company.subscription_end.isoformat() if company.subscription_end else "")
    return render_template("platform/company_form.html", company=company, form=form)


@platform_bp.route("/entreprises/<int:company_id>/statut", methods=["POST"])
@superadmin_required
def company_toggle(company_id):
    company = db.session.get(Company, company_id) or abort(404)
    company.is_active = not company.is_active
    audit("statut_entreprise", f"{company.name} -> {'active' if company.is_active else 'suspendue'}",
          company_id=company.id)
    db.session.commit()
    flash(f"Entreprise {'réactivée' if company.is_active else 'suspendue : ses utilisateurs ne peuvent plus se connecter'}.",
          "success")
    return redirect(url_for("platform.company_detail", company_id=company.id))


@platform_bp.route("/entreprises/<int:company_id>/prolonger", methods=["POST"])
@superadmin_required
def company_extend(company_id):
    company = db.session.get(Company, company_id) or abort(404)
    months = to_int(request.form.get("months"), 0)
    if months not in (1, 3, 6, 12):
        flash("Durée invalide.", "danger")
    else:
        start = max(company.subscription_end or utcnow().date(), utcnow().date())
        company.subscription_end = start + timedelta(days=30 * months)
        audit("prolongation_abonnement", f"{company.name} +{months} mois", company_id=company.id)
        db.session.commit()
        flash(f"Abonnement prolongé jusqu'au {company.subscription_end:%d/%m/%Y}.", "success")
    return redirect(url_for("platform.company_detail", company_id=company.id))


@platform_bp.route("/entreprises/<int:company_id>/utilisateurs/<int:user_id>/mot-de-passe", methods=["POST"])
@superadmin_required
def reset_user_password(company_id, user_id):
    user = db.session.get(User, user_id)
    if not user or user.company_id != company_id:
        abort(404)
    password = request.form.get("password", "")
    errors = _password_errors(password)
    if errors:
        for e in errors:
            flash(e, "danger")
    else:
        user.set_password(password)
        user.must_change_password = True
        user.failed_attempts = 0
        user.locked_until = None
        user.is_active_account = True
        audit("reinitialisation_mot_de_passe", user.username, company_id=company_id)
        db.session.commit()
        flash(f"Mot de passe de {user.username} réinitialisé (à changer à la prochaine connexion).", "success")
    return redirect(url_for("platform.company_detail", company_id=company_id))


@platform_bp.route("/entreprises/<int:company_id>/support", methods=["POST"])
@superadmin_required
def enter_support(company_id):
    company = db.session.get(Company, company_id) or abort(404)
    session["support_company_id"] = company.id
    audit("acces_support", f"Ouverture de l'espace {company.name}", company_id=company.id)
    db.session.commit()
    return redirect(url_for("dashboard.index"))


@platform_bp.route("/quitter-support", methods=["POST"])
@superadmin_required
def exit_support():
    session.pop("support_company_id", None)
    return redirect(url_for("platform.index"))


@platform_bp.route("/entreprises/<int:company_id>/logo")
@superadmin_required
def company_logo(company_id):
    from flask import Response
    company = db.session.get(Company, company_id) or abort(404)
    if not company.logo:
        abort(404)
    return Response(company.logo, mimetype=company.logo_mimetype or "image/png",
                    headers={"Cache-Control": "private, max-age=300"})


# ==================================================================================================
# FACTURE PRO (client libre, lignes, PDF, WhatsApp)
# ==================================================================================================

proinvoices_bp = Blueprint("proinvoices", __name__, url_prefix="/factures-pro")

PUBLIC_ENDPOINTS = {"proinvoices.public_pdf"}


def whatsapp_number(phone):
    """Numéro au format international sans « + » (Guinée par défaut : 224)."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 9 and digits.startswith("6"):
        digits = "224" + digits
    return digits


def _parse_day(value, default=None):
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return None


def _read_form(form):
    """Lit et vérifie le formulaire ; renvoie (valeurs, lignes, erreurs)."""
    errors = []
    values = {
        "doc_type": form.get("doc_type", "facture") if form.get("doc_type") in PRO_DOC_TYPES else "facture",
        "client_name": form.get("client_name", "").strip()[:150],
        "client_address": form.get("client_address", "").strip()[:1000],
        "client_phone": form.get("client_phone", "").strip()[:40],
        "client_email": form.get("client_email", "").strip()[:120],
        "client_nif": form.get("client_nif", "").strip()[:80],
        "payment_method": form.get("payment_method", "").strip()[:120],
        "notes": form.get("notes", "").strip()[:3000],
    }
    if not values["client_name"]:
        errors.append("Le nom du client est obligatoire.")
    issue = _parse_day(form.get("issue_date"), utcnow().date())
    due = _parse_day(form.get("due_date"))
    if issue is None:
        errors.append("Date de la facture invalide.")
    if due is None and form.get("due_date"):
        errors.append("Date d'échéance invalide.")
    if issue and due and due < issue:
        errors.append("L'échéance ne peut pas être avant la date de la facture.")
    values["issue_date"], values["due_date"] = issue, due

    lines = []
    designations = form.getlist("designation[]")
    quantities = form.getlist("quantity[]")
    prices = form.getlist("unit_price[]")
    for index, designation in enumerate(designations):
        designation = designation.strip()[:500]
        qty_raw = quantities[index] if index < len(quantities) else ""
        price_raw = prices[index] if index < len(prices) else ""
        if not designation and not str(price_raw).strip():
            continue  # ligne vide ignorée
        qty = to_float(qty_raw, -1)
        price = to_float(price_raw, -1)
        if not designation:
            errors.append(f"Ligne {index + 1} : la désignation est obligatoire.")
        if qty <= 0:
            errors.append(f"Ligne {index + 1} : quantité invalide.")
        if price < 0:
            errors.append(f"Ligne {index + 1} : prix unitaire invalide.")
        lines.append({"designation": designation, "quantity": qty, "unit_price": price})
    if not lines:
        errors.append("Ajoutez au moins une ligne (produit ou service).")

    subtotal = sum(max(l["quantity"], 0) * max(l["unit_price"], 0) for l in lines)
    discount = to_float(form.get("discount"), -1)
    tax_rate = to_float(form.get("tax_rate"), -1)
    paid = to_float(form.get("amount_paid"), -1)
    if discount < 0 or discount > subtotal:
        errors.append("La remise doit être comprise entre 0 et le sous-total.")
    if tax_rate < 0 or tax_rate > 100:
        errors.append("La TVA doit être comprise entre 0 et 100 %.")
    total = (subtotal - max(discount, 0)) * (1 + max(tax_rate, 0) / 100)
    if paid < 0 or paid > total + 0.01:
        errors.append("L'acompte doit être compris entre 0 et le total.")
    values.update(discount=max(discount, 0), tax_rate=max(tax_rate, 0), amount_paid=max(paid, 0))
    return values, lines, errors


def _apply(invoice, values, lines):
    for key, value in values.items():
        setattr(invoice, key, value)
    invoice.lines.clear()
    db.session.flush()
    for position, line in enumerate(lines):
        invoice.lines.append(ProInvoiceLine(position=position, **line))


def _get(invoice_id):
    invoice = db.session.get(ProInvoice, invoice_id)
    if invoice is None:
        abort(404)
    return invoice


def _form_context(invoice, form):
    products = Product.query.filter_by(is_active=True).order_by(Product.name).limit(500).all()
    if hasattr(form, "getlist"):  # formulaire renvoyé avec erreurs : on garde la saisie
        designations, qtys, prices = (form.getlist("designation[]"), form.getlist("quantity[]"),
                                      form.getlist("unit_price[]"))
        rows = [{"designation": d, "quantity": qtys[i] if i < len(qtys) else "1",
                 "unit_price": prices[i] if i < len(prices) else ""} for i, d in enumerate(designations)]
    elif invoice is not None:
        rows = [{"designation": l.designation, "quantity": f"{l.quantity:g}", "unit_price": f"{l.unit_price:g}"}
                for l in invoice.lines]
    else:
        rows = []
    if not rows:
        rows = [{"designation": "", "quantity": "1", "unit_price": ""}]
    values = {}
    if invoice is not None:
        values = {k: getattr(invoice, k) or "" for k in (
            "doc_type", "client_name", "client_address", "client_phone", "client_email", "client_nif",
            "payment_method", "notes")}
        values.update(issue_date=invoice.issue_date.isoformat(),
                      due_date=invoice.due_date.isoformat() if invoice.due_date else "",
                      discount=f"{invoice.discount:g}", tax_rate=f"{invoice.tax_rate:g}",
                      amount_paid=f"{invoice.amount_paid:g}")
    values.update({k: v for k, v in form.items() if not k.endswith("[]")} if form else {})
    return {"invoice": invoice, "v": values, "rows": rows, "doc_types": PRO_DOC_TYPES,
            "catalog": {p.name: p.sale_price for p in products}}


@proinvoices_bp.route("/", endpoint="index")
@login_required
def proinvoices_index():
    q = request.args.get("q", "").strip()
    doc_type = request.args.get("type", "")
    page = to_int(request.args.get("page"), 1) or 1
    query = ProInvoice.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(ProInvoice.client_name.ilike(like), ProInvoice.number.ilike(like),
                                    ProInvoice.client_phone.ilike(like)))
    if doc_type in PRO_DOC_TYPES:
        query = query.filter(ProInvoice.doc_type == doc_type)
    pagination = query.order_by(ProInvoice.created_at.desc()).paginate(page=page, per_page=25, error_out=False)
    return render_template("proinvoices/index.html", pagination=pagination, invoices=pagination.items,
                           q=q, doc_type=doc_type, doc_types=PRO_DOC_TYPES)


@proinvoices_bp.route("/nouvelle", methods=["GET", "POST"])
@proinvoices_bp.route("/<int:invoice_id>/modifier", methods=["GET", "POST"])
@login_required
def edit(invoice_id=None):
    invoice = _get(invoice_id) if invoice_id else None
    if request.method == "POST":
        values, lines, errors = _read_form(request.form)
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("proinvoices/form.html", **_form_context(invoice, request.form))
        creating = invoice is None
        if creating:
            prefix = PRO_DOC_TYPES[values["doc_type"]][1]
            invoice = ProInvoice(number=next_reference(ProInvoice, "number", prefix),
                                 share_token=secrets.token_urlsafe(24), user_id=current_user.id)
            db.session.add(invoice)
        _apply(invoice, values, lines)
        audit("facture_pro_creee" if creating else "facture_pro_modifiee", f"{invoice.number} {invoice.client_name}")
        db.session.commit()
        action = request.form.get("action", "save")
        flash(f"{invoice.title.capitalize()} {invoice.number} enregistrée.", "success")
        if action == "pdf":
            return redirect(url_for("proinvoices.detail", invoice_id=invoice.id, telecharger=1))
        if action == "whatsapp":
            return redirect(url_for("proinvoices.detail", invoice_id=invoice.id, whatsapp=1))
        return redirect(url_for("proinvoices.detail", invoice_id=invoice.id))

    form = {}
    if invoice is None:
        form = {"issue_date": utcnow().date().isoformat(), "tax_rate": get_setting("tax_rate") or "0",
                "doc_type": request.args.get("type", "facture")}
    return render_template("proinvoices/form.html", **_form_context(invoice, form))


@proinvoices_bp.route("/<int:invoice_id>", endpoint="detail")
@login_required
def proinvoices_detail(invoice_id):
    invoice = _get(invoice_id)
    currency = get_setting("currency") or "GNF"
    public_url = url_for("proinvoices.public_pdf", token=invoice.share_token, _external=True)
    message = (f"Bonjour {invoice.client_name},\n"
               f"Veuillez trouver votre {invoice.title.lower()} N° {invoice.number} "
               f"d'un montant de {format_amount(invoice.total, currency)}.\n"
               f"Consultez et téléchargez-la ici : {public_url}\n"
               f"Merci pour votre confiance.")
    phone = whatsapp_number(invoice.client_phone)
    wa_url = f"https://wa.me/{phone}?text={quote(message)}" if phone else f"https://wa.me/?text={quote(message)}"
    return render_template("proinvoices/detail.html", invoice=invoice, currency=currency, wa_url=wa_url,
                           wa_message=message, public_url=public_url,
                           auto_download=request.args.get("telecharger") == "1",
                           focus_whatsapp=request.args.get("whatsapp") == "1")


def _pdf_response(invoice, inline):
    data = build_pro_invoice_pdf(invoice)
    filename = f"{invoice.number}.pdf"
    disposition = "inline" if inline else "attachment"
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f'{disposition}; filename="{filename}"',
                             "Cache-Control": "private, no-store"})


@proinvoices_bp.route("/<int:invoice_id>/pdf")
@login_required
def pdf(invoice_id):
    return _pdf_response(_get(invoice_id), inline=request.args.get("voir") == "1")


@proinvoices_bp.route("/partage/<token>")
def public_pdf(token):
    """Lien envoyé au client par WhatsApp : ouvre le PDF sans connexion."""
    if not token or len(token) < 20:
        abort(404)
    invoice = (ProInvoice.query.execution_options(skip_tenant=True)
               .filter(ProInvoice.share_token == token).first())
    if invoice is None:
        abort(404)
    return _pdf_response(invoice, inline=True)


@proinvoices_bp.route("/<int:invoice_id>/nouveau-lien", methods=["POST"])
@login_required
def new_link(invoice_id):
    invoice = _get(invoice_id)
    invoice.share_token = secrets.token_urlsafe(24)
    audit("facture_pro_lien", invoice.number)
    db.session.commit()
    flash("Nouveau lien créé : l'ancien lien ne fonctionne plus.", "success")
    return redirect(url_for("proinvoices.detail", invoice_id=invoice.id))


@proinvoices_bp.route("/<int:invoice_id>/dupliquer", methods=["POST"])
@login_required
def duplicate(invoice_id):
    source = _get(invoice_id)
    copy = ProInvoice(number=next_reference(ProInvoice, "number", PRO_DOC_TYPES[source.doc_type][1]),
                      share_token=secrets.token_urlsafe(24), user_id=current_user.id,
                      doc_type=source.doc_type, issue_date=utcnow().date(), client_name=source.client_name,
                      client_address=source.client_address, client_phone=source.client_phone,
                      client_email=source.client_email, client_nif=source.client_nif,
                      payment_method=source.payment_method, discount=source.discount,
                      tax_rate=source.tax_rate, amount_paid=0, notes=source.notes)
    for line in source.lines:
        copy.lines.append(ProInvoiceLine(position=line.position, designation=line.designation,
                                         quantity=line.quantity, unit_price=line.unit_price))
    db.session.add(copy)
    audit("facture_pro_dupliquee", f"{source.number} -> {copy.number}")
    db.session.commit()
    flash(f"Copie créée : {copy.number}. Vous pouvez la modifier.", "success")
    return redirect(url_for("proinvoices.edit", invoice_id=copy.id))


@proinvoices_bp.route("/<int:invoice_id>/supprimer", methods=["POST"], endpoint="delete")
@role_required("gestionnaire")
def proinvoices_delete(invoice_id):
    invoice = _get(invoice_id)
    audit("facture_pro_supprimee", f"{invoice.number} {invoice.client_name}")
    db.session.delete(invoice)
    db.session.commit()
    flash("Document supprimé.", "success")
    return redirect(url_for("proinvoices.index"))


# ==================================================================================================
# CRÉATION DE L'APPLICATION
# ==================================================================================================

def create_app(config_class=Config):
    app = Flask(__name__, static_folder=None)
    app.config.from_object(config_class)
    app.jinja_loader = DictLoader(TEMPLATES)
    app.add_url_rule("/static/<path:filename>", endpoint="static", view_func=serve_static)
    # Render passe par un proxy HTTPS : liens externes corrects (https://…)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    init_tenancy()


    for blueprint in (auth_bp, dashboard_bp, products_bp, stock_bp, purchases_bp,
                      sales_bp, analytics_bp, archives_bp, settings_bp, platform_bp,
                      proinvoices_bp):
        app.register_blueprint(blueprint)

    _register_template_helpers(app)
    _register_security(app)
    _register_errors(app)
    _register_commands(app)

    with app.app_context():
        init_database(app)
        # Ferme les connexions ouvertes avant que gunicorn ne crée ses workers
        db.engine.dispose()

    return app


def _register_template_helpers(app):

    app.jinja_env.filters["money"] = money
    app.jinja_env.filters["en_lettres"] = amount_in_words
    app.jinja_env.filters["num"] = number
    app.jinja_env.filters["date"] = lambda d: d.strftime("%d/%m/%Y") if d else ""
    app.jinja_env.filters["datetime"] = lambda d: d.strftime("%d/%m/%Y %H:%M") if d else ""

    def form_lines(form):
        """Reconstitue les lignes produit d'un formulaire renvoyé avec erreurs."""
        if not hasattr(form, "getlist"):
            return []
        _db = db
        lines = []
        ids, qtys, prices = form.getlist("product_id[]"), form.getlist("quantity[]"), form.getlist("price[]")
        for i, raw in enumerate(ids):
            product = _db.session.get(Product, to_int(raw) or 0)
            if product:
                lines.append({"id": product.id, "sku": product.sku, "name": product.name,
                              "stock": product.quantity, "unit": product.unit,
                              "qty": to_float(qtys[i] if i < len(qtys) else 1, 1),
                              "price": to_float(prices[i] if i < len(prices) else 0, 0)})
        return lines

    app.jinja_env.globals["form_lines"] = form_lines

    @app.context_processor
    def inject_globals():
        data = {"app_name": app.config["APP_NAME"], "now": utcnow(), "in_platform": False,
                "company": None, "support_mode": False}
        if current_user.is_authenticated:
            settings = all_settings()
            data["settings"] = settings
            data["company"] = current_company()
            data["in_platform"] = data["company"] is None
            data["support_mode"] = current_user.role == "superadmin" and data["company"] is not None
            if data["in_platform"]:
                data["unread_count"] = 0
                data["low_stock_count"] = 0
                return data
            data["unread_count"] = Message.query.filter(
                Message.recipient_id == current_user.id,
                Message.is_read.is_(False),
            ).count()
            data["low_stock_count"] = Product.query.filter(
                Product.is_active.is_(True), Product.quantity <= Product.min_stock
            ).count() if settings.get("low_stock_alert") == "1" else 0
        return data


def _register_security(app):
    @app.before_request
    def set_tenant():
        """Détermine l'entreprise active pour toute la requête."""
        g.company_id = None
        if request.endpoint in ("static", "proinvoices.public_pdf") or not current_user.is_authenticated:
            return None
        if current_user.role == "superadmin":
            if request.blueprint != "platform":
                g.company_id = session.get("support_company_id")
            if g.company_id is None and request.blueprint not in ("platform", "auth") \
                    and request.endpoint is not None:
                return redirect(url_for("platform.index"))
            return None
        company = current_user.company
        if company is None or not company.can_login:
            reason = "expiré" if company is not None and company.is_expired else "suspendu"
            logout_user()
            flash(f"L'abonnement de votre entreprise est {reason}. Contactez ANSARTECH 224.", "danger")
            return redirect(url_for("auth.login"))
        g.company_id = company.id
        return None

    @app.before_request
    def force_password_change():
        if (current_user.is_authenticated and current_user.must_change_password
                and request.endpoint not in ("auth.change_password", "auth.logout", "static", "proinvoices.public_pdf")):
            return redirect(url_for("auth.change_password"))

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if current_user.is_authenticated and response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store"
        return response


def _register_errors(app):
    from flask_wtf.csrf import CSRFError

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("errors/error.html", code=403,
                               message="Vous n'avez pas les droits pour accéder à cette page."), 403

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("errors/error.html", code=404, message="Page introuvable."), 404

    @app.errorhandler(413)
    def too_large(_e):
        return render_template("errors/error.html", code=413,
                               message="Fichier trop volumineux (5 Mo maximum)."), 413

    @app.errorhandler(CSRFError)
    def csrf_error(_e):
        return render_template("errors/error.html", code=400,
                               message="Session expirée. Rechargez la page et réessayez."), 400

    @app.errorhandler(500)
    def server_error(_e):
        db.session.rollback()
        return render_template("errors/error.html", code=500,
                               message="Erreur interne. L'opération n'a pas été enregistrée."), 500


def _register_commands(app):
    @app.cli.command("init-db")
    def init_db_command():
        """Crée les tables et le compte administrateur."""
        init_database(app)
        click.echo("Base de données prête.")

    @app.cli.command("demo-data")
    def demo_data_command():
        """Crée une entreprise de démonstration avec des données d'exemple."""
        create_demo_company()
        click.echo("Données de démonstration ajoutées.")

    @app.cli.command("reset-admin")
    @click.argument("password")
    def reset_admin(password):
        """Réinitialise le mot de passe du super-administrateur de la plateforme."""
        user = User.query.filter_by(role="superadmin").order_by(User.id).first()
        if not user:
            click.echo("Administrateur introuvable.")
            return
        user.set_password(password)
        user.failed_attempts = 0
        user.locked_until = None
        user.is_active_account = True
        db.session.commit()
        click.echo("Mot de passe administrateur réinitialisé.")


# ==================================================================================================
# PAGES HTML (templates)
# ==================================================================================================

TEMPLATES = {

# ---------------------------------------------------------------- _flash.html
'_flash.html': r'''{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
  <div class="flashes">
    {% for category, message in messages %}
      <div class="alert alert-{{ category if category in ('success','danger','warning','info') else 'info' }}">
        <span>{{ message }}</span><button type="button" class="alert-close" aria-label="Fermer">×</button>
      </div>
    {% endfor %}
  </div>
  {% endif %}
{% endwith %}
''',

# ---------------------------------------------------------------- _line_editor.html
'_line_editor.html': r'''{# Éditeur de lignes partagé par les ventes et les achats. Variables : mode, price_label #}
<div class="card flush" data-line-editor="{{ mode }}" data-search-url="{{ url_for('products.api_search') }}"
     data-currency="{{ settings.currency }}" data-tax-rate="{{ tax_rate if tax_rate is defined else 0 }}"
     data-initial='{{ form_lines(form)|tojson }}'>
  <div class="card-head"><h2>Produits</h2></div>
  <div style="padding:0 1.25rem 1rem">
    <div class="search-box">
      <input type="search" id="productSearch" placeholder="🔍 Rechercher un produit ou scanner un code-barres…" autocomplete="off" aria-label="Rechercher un produit">
      <div class="search-results" id="searchResults" hidden></div>
    </div>
  </div>
  <div class="table-wrap">
    <table class="line-table">
      <thead><tr><th>Produit</th><th style="width:110px">Quantité</th><th style="width:150px">{{ price_label }}</th><th class="num">Total</th><th></th></tr></thead>
      <tbody id="lines"></tbody>
    </table>
  </div>
  <div class="empty" id="emptyLines">Aucun produit ajouté. Utilisez la recherche ci-dessus.</div>
</div>
''',

# ---------------------------------------------------------------- _macros.html
'_macros.html': r'''{% macro csrf() %}<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">{% endmacro %}

{% macro pager(pagination, endpoint) %}
{% if pagination.pages > 1 %}
<nav class="pager">
  {% set args = request.args.to_dict() %}
  {% if pagination.has_prev %}{% set _ = args.update({'page': pagination.prev_num}) %}<a class="btn btn-light btn-sm" href="{{ url_for(endpoint, **args) }}">‹ Précédent</a>{% endif %}
  <span>Page {{ pagination.page }} / {{ pagination.pages }}</span>
  {% if pagination.has_next %}{% set _ = args.update({'page': pagination.next_num}) %}<a class="btn btn-light btn-sm" href="{{ url_for(endpoint, **args) }}">Suivant ›</a>{% endif %}
</nav>
{% endif %}
{% endmacro %}

{% macro sale_badge(status, label) %}
<span class="badge {{ {'payee':'badge-success','partielle':'badge-warning','impayee':'badge-danger','annulee':'badge-muted'}[status] }}">{{ label }}</span>
{% endmacro %}

{% macro purchase_badge(status, label) %}
<span class="badge {{ {'recu':'badge-success','commande':'badge-info','annule':'badge-muted'}[status] }}">{{ label }}</span>
{% endmacro %}

{% macro stock_badge(product) %}
{% if product.stock_status == 'rupture' %}<span class="badge badge-danger">Rupture</span>
{% elif product.stock_status == 'bas' %}<span class="badge badge-warning">Stock bas</span>
{% else %}<span class="badge badge-success">OK</span>{% endif %}
{% endmacro %}

{% macro empty(text) %}<div class="empty">{{ text }}</div>{% endmacro %}
''',

# ---------------------------------------------------------------- analytics/index.html
'analytics/index.html': r'''{% extends "base.html" %}
{% from "_macros.html" import empty with context %}
{% block title %}Analyses{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Analyses &amp; recommandations</h1><p>Performance commerciale et conseils automatiques.</p></div>
  <div class="actions"><a class="btn btn-light" href="{{ url_for('analytics.export_reorder', jours=days) }}">Exporter la commande suggérée</a></div>
</div>
<div class="tabs">
  {% for d, label in [(7,'7 jours'),(30,'30 jours'),(90,'3 mois'),(180,'6 mois'),(365,'12 mois')] %}
  <a href="{{ url_for('analytics.index', jours=d) }}" class="{{ 'active' if d == days }}">{{ label }}</a>{% endfor %}
</div>

<div class="kpis">
  <div class="kpi"><div class="label">Chiffre d'affaires</div><div class="value">{{ summary.revenue|money }}</div><div class="sub">{{ summary.count }} vente(s)</div></div>
  <div class="kpi k-accent"><div class="label">Bénéfice brut</div><div class="value">{{ summary.profit|money }}</div><div class="sub">Marge : {{ '%.1f'|format(summary.margin) }} %</div></div>
  <div class="kpi k-blue"><div class="label">Panier moyen</div><div class="value">{{ summary.basket|money }}</div><div class="sub">par vente</div></div>
  <div class="kpi k-red"><div class="label">À encaisser</div><div class="value">{{ summary.receivables|money }}</div><div class="sub">créances clients</div></div>
</div>

<div class="card">
  <div class="card-head"><h2>💡 Recommandations</h2></div>
  {% for level, text in rec.advice %}<div class="advice {{ level }}">{{ text }}</div>{% endfor %}
</div>

<div class="card flush">
  <div class="card-head"><h2>Réapprovisionnement suggéré</h2><small>Objectif : 30 jours de stock</small></div>
  {% if rec.reorder %}
  <div class="table-wrap"><table>
    <thead><tr><th>Produit</th><th class="num">Stock</th><th class="num hide-sm">Ventes / jour</th><th class="num">Jours restants</th><th class="num">À commander</th><th class="num hide-sm">Coût estimé</th></tr></thead>
    <tbody>{% for r in rec.reorder %}
      <tr><td><a href="{{ url_for('products.detail', product_id=r.product.id) }}">{{ r.product.name }}</a> {% if r.urgent %}<span class="badge badge-danger">Urgent</span>{% endif %}</td>
        <td class="num">{{ r.product.quantity|num }}</td><td class="num hide-sm">{{ '%.2f'|format(r.daily) }}</td>
        <td class="num">{{ r.cover|round|int if r.cover is not none else '—' }}</td>
        <td class="num"><strong>{{ r.qty|num }}</strong></td><td class="num hide-sm">{{ r.cost|money }}</td></tr>
    {% endfor %}</tbody>
    <tfoot><tr><td colspan="4">Budget total estimé</td><td></td><td class="num hide-sm">{{ rec.reorder|sum(attribute='cost')|money }}</td></tr></tfoot>
  </table></div>
  {% else %}{{ empty("Aucun réapprovisionnement nécessaire.") }}{% endif %}
</div>

<div class="grid grid-main">
  <div class="card">
    <div class="card-head"><h2>Chiffre d'affaires et bénéfice (12 mois)</h2></div>
    <div class="chart-box"><canvas id="monthChart"></canvas></div>
  </div>
  <div class="card">
    <div class="card-head"><h2>Ventes par catégorie</h2></div>
    {% if chart.categories %}<div class="chart-box sm"><canvas id="catChart"></canvas></div>{% else %}{{ empty("Pas de données.") }}{% endif %}
  </div>
</div>

<div class="grid grid-2">
  <div class="card">
    <div class="card-head"><h2>Ventes par jour de la semaine</h2></div>
    <div class="chart-box sm"><canvas id="dayChart"></canvas></div>
  </div>
  <div class="card">
    <div class="card-head"><h2>Modes de paiement</h2></div>
    {% if chart.payments %}
    <ul class="list-plain">{% for k, v in chart.payments.items() %}<li><span>{{ k }}</span><strong>{{ v|money }}</strong></li>{% endfor %}</ul>
    {% else %}{{ empty("Pas de données.") }}{% endif %}
  </div>
</div>

<div class="card flush">
  <div class="card-head"><h2>Top 10 des produits</h2></div>
  {% if top_products %}
  <div class="table-wrap"><table>
    <thead><tr><th>#</th><th>Produit</th><th class="num">Qté vendue</th><th class="num">CA</th><th class="num hide-sm">Bénéfice</th></tr></thead>
    <tbody>{% for t in top_products %}
      <tr><td>{{ loop.index }}</td><td><a href="{{ url_for('products.detail', product_id=t.product.id) }}">{{ t.product.name }}</a></td>
        <td class="num">{{ t.qty|num }}</td><td class="num">{{ t.revenue|money }}</td><td class="num hide-sm">{{ t.profit|money }}</td></tr>
    {% endfor %}</tbody>
  </table></div>
  {% else %}{{ empty("Aucune vente sur la période.") }}{% endif %}
</div>

<div class="grid grid-2">
  <div class="card">
    <h2>Stock dormant (aucune vente sur la période)</h2>
    {% if rec.dormant %}<ul class="list-plain">{% for p in rec.dormant %}<li><a href="{{ url_for('products.detail', product_id=p.id) }}">{{ p.name }}</a><span>{{ p.quantity|num }} · {{ p.stock_value|money }}</span></li>{% endfor %}</ul>
    {% else %}{{ empty("Tous les produits en stock se vendent.") }}{% endif %}
  </div>
  <div class="card">
    <h2>Marges faibles (&lt; 10 %)</h2>
    {% if rec.low_margin %}<ul class="list-plain">{% for p in rec.low_margin %}<li><a href="{{ url_for('products.detail', product_id=p.id) }}">{{ p.name }}</a><span>{{ '%.1f'|format(p.margin_rate) }} %</span></li>{% endfor %}</ul>
    {% else %}{{ empty("Toutes les marges sont correctes.") }}{% endif %}
  </div>
</div>
{% endblock %}
{% block scripts %}
<script src="{{ url_for('static', filename='js/chart.umd.min.js') }}"></script>
<script>
(function () {
  if (!window.Chart) return;
  var cur = " {{ settings.currency }}";
  var short = function (v) { return v >= 1e6 ? (v / 1e6) + " M" : (v >= 1e3 ? (v / 1e3) + " k" : v); };
  var tip = { callbacks: { label: function (c) { return (c.dataset.label ? c.dataset.label + " : " : "") + ansarFmt(c.parsed.y !== undefined ? c.parsed.y : c.parsed) + cur; } } };
  new Chart(document.getElementById("monthChart"), {
    type: "bar",
    data: { labels: {{ chart.labels|tojson }}, datasets: [
      { label: "Chiffre d'affaires", data: {{ chart.revenue|tojson }}, backgroundColor: "#0b5d43", borderRadius: 5 },
      { label: "Bénéfice", data: {{ chart.profit|tojson }}, backgroundColor: "#f2b705", borderRadius: 5 }] },
    options: { responsive: true, maintainAspectRatio: false, plugins: { tooltip: tip, legend: { position: "bottom" } },
      scales: { y: { beginAtZero: true, ticks: { callback: short } }, x: { grid: { display: false } } } }
  });
  var cat = document.getElementById("catChart");
  if (cat) new Chart(cat, {
    type: "doughnut",
    data: { labels: {{ chart.categories.keys()|list|tojson }}, datasets: [{ data: {{ chart.categories.values()|list|tojson }},
      backgroundColor: ["#0b5d43", "#f2b705", "#1d5fa8", "#c62f2f", "#7a8f85", "#14a37f", "#b86e00", "#6b4fa8"] }] },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "bottom" },
      tooltip: { callbacks: { label: function (c) { return c.label + " : " + ansarFmt(c.parsed) + cur; } } } } }
  });
  new Chart(document.getElementById("dayChart"), {
    type: "bar",
    data: { labels: {{ chart.weekdays.labels|tojson }}, datasets: [{ data: {{ chart.weekdays.data|tojson }}, backgroundColor: "#1d5fa8", borderRadius: 5 }] },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: tip },
      scales: { y: { beginAtZero: true, ticks: { callback: short } }, x: { grid: { display: false } } } }
  });
})();
</script>
{% endblock %}
''',

# ---------------------------------------------------------------- archives/compose.html
'archives/compose.html': r'''{% extends "base.html" %}
{% block title %}Nouveau message{% endblock %}
{% block content %}
{% set v = form if form else {} %}
<div class="page-head"><div><h1>Nouveau message</h1></div></div>
<div class="card" style="max-width:760px">
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="field"><label for="recipient_id">Destinataire <span class="req">*</span></label>
      <select id="recipient_id" name="recipient_id" required>
        <option value="">— Choisir —</option>
        <option value="all" {{ 'selected' if v.get('recipient_id') == 'all' }}>📢 Toute l'équipe (annonce)</option>
        {% for u in users %}<option value="{{ u.id }}" {{ 'selected' if v.get('recipient_id') == u.id|string }}>{{ u.full_name }} — {{ u.role_label }}</option>{% endfor %}
      </select></div>
    <div class="field" style="margin-top:1rem"><label for="subject">Sujet <span class="req">*</span></label><input type="text" id="subject" name="subject" value="{{ v.get('subject','') }}" maxlength="200" required></div>
    <div class="field" style="margin-top:1rem"><label for="body">Message <span class="req">*</span></label><textarea id="body" name="body" rows="7" required>{{ v.get('body','') }}</textarea></div>
    <div class="form-actions">
      <a class="btn btn-light" href="{{ url_for('archives.messages') }}">Annuler</a>
      <button class="btn" type="submit">Envoyer</button>
    </div>
  </form>
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- archives/documents.html
'archives/documents.html': r'''{% extends "base.html" %}
{% from "_macros.html" import empty with context %}
{% block title %}Archives{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Archives &amp; documents</h1><p>Factures fournisseurs, contrats, pièces administratives… (5 Mo max par fichier)</p></div>
  <div class="actions">
    <a class="btn btn-light" href="{{ url_for('sales.export') }}">Archive des ventes (CSV)</a>
    <a class="btn btn-light" href="{{ url_for('purchases.export') }}">Archive des achats (CSV)</a>
  </div>
</div>

<div class="card">
  <h2>Ajouter un document</h2>
  <form method="post" action="{{ url_for('archives.upload') }}" enctype="multipart/form-data">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="form-grid">
      <div class="field"><label for="file">Fichier <span class="req">*</span></label>
        <input type="file" id="file" name="file" required accept=".pdf,.png,.jpg,.jpeg,.gif,.webp,.doc,.docx,.xls,.xlsx,.csv,.txt,.odt,.ods"></div>
      <div class="field"><label for="title">Titre</label><input type="text" id="title" name="title" maxlength="200" placeholder="Laissez vide pour utiliser le nom du fichier"></div>
      <div class="field"><label for="category">Catégorie</label>
        <select id="category" name="category">{% for k, label in categories.items() %}<option value="{{ k }}">{{ label }}</option>{% endfor %}</select></div>
      <div class="field"><label for="description">Description</label><input type="text" id="description" name="description"></div>
    </div>
    <div class="form-actions"><button class="btn" type="submit">Archiver le document</button></div>
  </form>
</div>

<form class="filters" method="get">
  <input type="search" name="q" value="{{ q }}" placeholder="Rechercher un document…" aria-label="Rechercher">
  <select name="categorie" aria-label="Catégorie"><option value="">Toutes les catégories</option>
    {% for k, label in categories.items() %}<option value="{{ k }}" {{ 'selected' if k == category }}>{{ label }}</option>{% endfor %}</select>
  <button class="btn" type="submit">Filtrer</button>
</form>

<div class="card flush">
  {% if documents %}
  <div class="table-wrap"><table>
    <thead><tr><th>Document</th><th class="hide-sm">Catégorie</th><th class="hide-sm">Ajouté par</th><th class="hide-sm">Date</th><th class="num hide-sm">Taille</th><th></th></tr></thead>
    <tbody>{% for d in documents %}
      <tr><td><span class="cell-title">{{ d.title }}</span><span class="cell-sub">{{ d.original_name }}</span></td>
        <td class="hide-sm">{{ categories.get(d.category, d.category) }}</td>
        <td class="hide-sm">{{ users.get(d.uploaded_by, '—') }}</td>
        <td class="hide-sm nowrap">{{ d.created_at|date }}</td>
        <td class="num hide-sm">{{ '%.0f'|format(d.size / 1024) }} Ko</td>
        <td class="nowrap" style="text-align:right">
          <a class="btn btn-light btn-sm" href="{{ url_for('archives.download', doc_id=d.id, voir=1) }}" target="_blank" rel="noopener">Ouvrir</a>
          {% if current_user.effective_role in ('admin','gestionnaire') or d.uploaded_by == current_user.id %}
          <form class="inline" method="post" action="{{ url_for('archives.delete', doc_id=d.id) }}" data-confirm="Supprimer ce document ?">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <button class="btn btn-ghost-danger btn-sm" type="submit" aria-label="Supprimer">✕</button>
          </form>{% endif %}
        </td></tr>
    {% endfor %}</tbody>
  </table></div>
  {% else %}{{ empty("Aucun document archivé.") }}{% endif %}
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- archives/message_detail.html
'archives/message_detail.html': r'''{% extends "base.html" %}
{% block title %}{{ message.subject }}{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>{{ message.subject }}</h1>
    <p>De <strong>{{ message.sender.full_name if message.sender else 'Support ANSARTECH 224' }}</strong> à <strong>{{ message.recipient.full_name if message.recipient else "toute l'équipe" }}</strong> · {{ message.created_at|datetime }}</p></div>
  <div class="actions">
    <a class="btn btn-light" href="{{ url_for('archives.messages') }}">← Messages</a>
    {% if message.sender_id != current_user.id %}<a class="btn" href="{{ url_for('archives.compose', repondre=message.id) }}">Répondre</a>{% endif %}
  </div>
</div>
<div class="card" style="max-width:860px">
  <div class="message-body">{{ message.body }}</div>
  {% if current_user.effective_role == 'admin' %}
  <form method="post" action="{{ url_for('archives.delete_message', message_id=message.id) }}" data-confirm="Supprimer ce message ?" style="margin-top:1.5rem">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <button class="btn btn-ghost-danger btn-sm" type="submit">Supprimer</button>
  </form>{% endif %}
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- archives/messages.html
'archives/messages.html': r'''{% extends "base.html" %}
{% from "_macros.html" import empty with context %}
{% block title %}Messages{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Messagerie interne</h1><p>Échanges entre les membres de l'équipe.</p></div>
  <div class="actions">
    {% if box != 'envoyes' and unread_count %}
    <form method="post" action="{{ url_for('archives.mark_all_read') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn btn-light" type="submit">Tout marquer comme lu</button></form>
    {% endif %}
    <a class="btn" href="{{ url_for('archives.compose') }}">＋ Nouveau message</a>
  </div>
</div>
<div class="tabs">
  <a href="{{ url_for('archives.messages') }}" class="{{ 'active' if box != 'envoyes' }}">Reçus{% if unread_count %} ({{ unread_count }}){% endif %}</a>
  <a href="{{ url_for('archives.messages', boite='envoyes') }}" class="{{ 'active' if box == 'envoyes' }}">Envoyés</a>
</div>
<div class="card flush">
  {% if messages %}
  <div class="table-wrap"><table>
    <thead><tr><th>{{ 'À' if box == 'envoyes' else 'De' }}</th><th>Sujet</th><th class="hide-sm">Date</th></tr></thead>
    <tbody>{% for m in messages %}
      <tr class="{{ 'unread' if box != 'envoyes' and not m.is_read and m.recipient_id == current_user.id }}">
        <td class="nowrap">{% if box == 'envoyes' %}{{ m.recipient.full_name if m.recipient else "Toute l'équipe" }}{% else %}{{ m.sender.full_name if m.sender else 'Support ANSARTECH 224' }}{% endif %}</td>
        <td><a href="{{ url_for('archives.message_detail', message_id=m.id) }}">{{ m.subject }}</a>{% if not m.recipient_id %} <span class="badge badge-info">Annonce</span>{% endif %}
          <span class="cell-sub">{{ m.body[:90] }}{{ '…' if m.body|length > 90 }}</span></td>
        <td class="hide-sm nowrap">{{ m.created_at|datetime }}</td></tr>
    {% endfor %}</tbody>
  </table></div>
  {% else %}{{ empty("Aucun message.") }}{% endif %}
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- auth/_pw_form.html
'auth/_pw_form.html': r'''<form method="post" action="{{ url_for('auth.change_password') }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <div class="field"><label for="cp">Mot de passe actuel</label><input type="password" id="cp" name="current_password" autocomplete="current-password" required></div>
  <div class="field"><label for="np">Nouveau mot de passe</label><input type="password" id="np" name="new_password" autocomplete="new-password" minlength="8" required>
    <div class="hint">8 caractères minimum, avec des lettres et des chiffres.</div></div>
  <div class="field"><label for="cf">Confirmer le nouveau mot de passe</label><input type="password" id="cf" name="confirm_password" autocomplete="new-password" required></div>
  <div class="form-actions"><button class="btn {{ 'btn-block' if current_user.must_change_password }}" type="submit">Enregistrer le mot de passe</button></div>
</form>
''',

# ---------------------------------------------------------------- auth/change_password.html
'auth/change_password.html': r'''{% extends "base.html" %}
{% block title %}Mot de passe{% endblock %}

{% block auth_content %}
<div class="auth-card">
  <div class="logo"><img src="{{ url_for('static', filename='img/logo.svg') }}" alt=""><h1>Sécurisez votre compte</h1>
    <small>Pour votre première connexion, choisissez un nouveau mot de passe personnel.</small></div>
  {% include "auth/_pw_form.html" %}
  <form method="post" action="{{ url_for('auth.logout') }}" style="text-align:center;margin-top:.75rem">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <button class="btn btn-light btn-sm" type="submit">Se déconnecter</button>
  </form>
</div>
{% endblock %}

{% block content %}
<div class="page-head"><div><h1>Changer mon mot de passe</h1></div></div>
<div class="card" style="max-width:520px">{% include "auth/_pw_form.html" %}</div>
{% endblock %}
''',

# ---------------------------------------------------------------- auth/login.html
'auth/login.html': r'''{% extends "base.html" %}
{% block title %}Connexion{% endblock %}
{% block auth_content %}
<div class="auth-card">
  <div class="logo">
    <img src="{{ url_for('static', filename='img/logo.svg') }}" alt="">
    <h1>ANSARTECH 224</h1>
    <small>Gestion commerciale · stocks · ventes</small>
  </div>
  {% if config.DB_SCHEMA_ERROR %}<div class="alert alert-danger" style="margin-bottom:1rem"><span>{{ config.DB_SCHEMA_ERROR }}</span></div>{% endif %}
  <form method="post" autocomplete="on">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="field">
      <label for="username">Identifiant</label>
      <input type="text" id="username" name="username" value="{{ username or '' }}" autocapitalize="none" autocomplete="username" required autofocus>
    </div>
    <div class="field">
      <label for="password">Mot de passe</label>
      <div class="pw-wrap">
        <input type="password" id="password" name="password" autocomplete="current-password" required>
        <button type="button" class="pw-toggle" aria-label="Afficher le mot de passe"
                onclick="var i=document.getElementById('password');var show=i.type==='password';i.type=show?'text':'password';this.textContent=show?'Masquer':'Afficher';">Afficher</button>
      </div>
    </div>
    <div class="field"><label class="check"><input type="checkbox" name="remember" value="1"> Rester connecté</label></div>
    <button class="btn btn-block" type="submit">Se connecter</button>
  </form>
</div>
<p class="auth-foot">© {{ now.year }} ANSARTECH 224 · Conakry</p>
{% endblock %}
''',

# ---------------------------------------------------------------- auth/profile.html
'auth/profile.html': r'''{% extends "base.html" %}
{% block title %}Mon profil{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Mon profil</h1><p>{{ current_user.role_label }} · identifiant <strong>{{ current_user.username }}</strong></p></div>
  <div class="actions"><a class="btn btn-light" href="{{ url_for('auth.change_password') }}">Changer mon mot de passe</a></div>
</div>
<div class="card" style="max-width:720px">
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="form-grid">
      <div class="field full"><label for="full_name">Nom complet <span class="req">*</span></label><input type="text" id="full_name" name="full_name" value="{{ current_user.full_name }}" required></div>
      <div class="field"><label for="email">E-mail</label><input type="email" id="email" name="email" value="{{ current_user.email or '' }}"></div>
      <div class="field"><label for="phone">Téléphone</label><input type="tel" id="phone" name="phone" value="{{ current_user.phone or '' }}"></div>
    </div>
    <div class="form-actions"><button class="btn" type="submit">Enregistrer</button></div>
  </form>
</div>
<div class="card" style="max-width:720px">
  <dl class="dl"><dt>Dernière connexion</dt><dd>{{ current_user.last_login|datetime or '—' }}</dd>
  <dt>Compte créé le</dt><dd>{{ current_user.created_at|date }}</dd></dl>
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- auth/user_form.html
'auth/user_form.html': r'''{% extends "base.html" %}
{% block title %}{{ 'Modifier' if user else 'Nouvel' }} utilisateur{% endblock %}
{% block content %}
{% set v = form if form else {} %}
<div class="page-head"><div><h1>{{ 'Modifier ' ~ user.full_name if user else 'Nouvel utilisateur' }}</h1>
  <p>{{ "Laissez le mot de passe vide pour le conserver." if user else "L'utilisateur devra changer ce mot de passe à sa première connexion." }}</p></div></div>
<div class="card" style="max-width:760px">
  <form method="post" autocomplete="off">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="form-grid">
      <div class="field"><label for="full_name">Nom complet <span class="req">*</span></label>
        <input type="text" id="full_name" name="full_name" value="{{ v.get('full_name', user.full_name if user else '') }}" required></div>
      <div class="field"><label for="username">Identifiant <span class="req">*</span></label>
        <input type="text" id="username" name="username" value="{{ v.get('username', user.username if user else '') }}" autocapitalize="none" pattern="[a-zA-Z0-9._\-]{3,50}" required>
        <div class="hint">Ex. : m.diallo</div></div>
      <div class="field"><label for="email">E-mail</label><input type="email" id="email" name="email" value="{{ v.get('email', (user.email or '') if user else '') }}"></div>
      <div class="field"><label for="phone">Téléphone</label><input type="tel" id="phone" name="phone" value="{{ v.get('phone', (user.phone or '') if user else '') }}"></div>
      <div class="field"><label for="role">Rôle <span class="req">*</span></label>
        <select id="role" name="role">
          {% set current_role = v.get('role', user.role if user else 'vendeur') %}
          {% for key, label in roles.items() %}<option value="{{ key }}" {{ 'selected' if key == current_role }}>{{ label }}</option>{% endfor %}
        </select></div>
      <div class="field"><label for="password">Mot de passe {% if not user %}<span class="req">*</span>{% endif %}</label>
        <input type="password" id="password" name="password" autocomplete="new-password" {{ '' if user else 'required' }} minlength="8">
        <div class="hint">8 caractères minimum, lettres et chiffres.</div></div>
    </div>
    <div class="form-actions">
      <a class="btn btn-light" href="{{ url_for('auth.users') }}">Annuler</a>
      <button class="btn" type="submit">Enregistrer</button>
    </div>
  </form>
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- auth/users.html
'auth/users.html': r'''{% extends "base.html" %}
{% block title %}Utilisateurs{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Utilisateurs</h1><p>Comptes, rôles et accès au logiciel.</p></div>
  <div class="actions"><a class="btn" href="{{ url_for('auth.user_form') }}">＋ Nouvel utilisateur</a></div>
</div>
<div class="card">
  <ul class="list-plain" style="margin-bottom:.5rem">
    <li><strong>Administrateur</strong><span class="muted">Accès complet : utilisateurs, paramètres, suppressions, journal.</span></li>
    <li><strong>Gestionnaire</strong><span class="muted">Produits, stocks, achats, ventes, analyses.</span></li>
    <li><strong>Vendeur</strong><span class="muted">Ventes, clients, consultation des produits et stocks.</span></li>
  </ul>
</div>
<div class="card flush">
  <div class="table-wrap">
  <table>
    <thead><tr><th>Nom</th><th>Rôle</th><th class="hide-sm">Dernière connexion</th><th>Statut</th><th></th></tr></thead>
    <tbody>
    {% for u in users %}
      <tr class="{{ 'row-muted' if not u.is_active_account }}">
        <td><span class="cell-title">{{ u.full_name }}</span><span class="cell-sub">{{ u.username }}{% if u.phone %} · {{ u.phone }}{% endif %}</span></td>
        <td>{{ u.role_label }}</td>
        <td class="hide-sm">{{ u.last_login|datetime or 'Jamais' }}</td>
        <td>{% if u.is_active_account %}<span class="badge badge-success">Actif</span>{% else %}<span class="badge badge-muted">Désactivé</span>{% endif %}
            {% if u.locked_until and u.locked_until > now %}<span class="badge badge-danger">Bloqué</span>{% endif %}</td>
        <td class="nowrap" style="text-align:right">
          <a class="btn btn-light btn-sm" href="{{ url_for('auth.user_form', user_id=u.id) }}">Modifier</a>
          {% if u.id != current_user.id %}
          <form class="inline" method="post" action="{{ url_for('auth.toggle_user', user_id=u.id) }}" data-confirm="Confirmer le changement de statut de {{ u.full_name }} ?">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <button class="btn btn-sm {{ 'btn-ghost-danger' if u.is_active_account else 'btn-light' }}" type="submit">{{ 'Désactiver' if u.is_active_account else 'Activer' }}</button>
          </form>
          {% endif %}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- base.html
'base.html': r'''<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#0b5d43">
  <title>{% block title %}{% endblock %} · {{ app_name }}</title>
  <link rel="icon" href="{{ url_for('static', filename='img/logo.svg') }}" type="image/svg+xml">
  <link rel="manifest" href="{{ url_for('static', filename='manifest.json') }}">
  <link rel="stylesheet" href="{{ url_for('static', filename='css/style.css') }}">
  {% block head %}{% endblock %}
</head>
<body class="{% block body_class %}{% endblock %}">
{% if current_user.is_authenticated and not current_user.must_change_password %}
  {% set ep = request.endpoint or '' %}
  <header class="topbar">
    <button class="icon-btn" id="menuToggle" aria-label="Ouvrir le menu">☰</button>
    <a class="brand-sm" href="{{ url_for('platform.index') if in_platform else url_for('dashboard.index') }}">{% if company and company.logo %}<img src="{{ url_for('settings.logo', v=company.id) }}" alt="">{% else %}<img src="{{ url_for('static', filename='img/logo.svg') }}" alt="">{% endif %}<span class="brand-text">{{ company.name if company else 'ANSARTECH 224' }}</span></a>
    {% if in_platform %}<span class="icon-btn" aria-hidden="true"></span>{% else %}
    <a class="icon-btn" href="{{ url_for('archives.messages') }}" aria-label="Messages">✉{% if unread_count %}<span class="dot">{{ unread_count }}</span>{% endif %}</a>{% endif %}
  </header>

  <aside class="sidebar" id="sidebar">
    <a class="brand" href="{{ url_for('platform.index') if in_platform else url_for('dashboard.index') }}">
      <img src="{{ url_for('static', filename='img/logo.svg') }}" alt="">
      <span><strong>ANSARTECH</strong> 224</span>
    </a>
    {% if company %}
    <div class="company-chip">
      {% if company.logo %}<img src="{{ url_for('settings.logo', v=company.id) }}" alt="">{% else %}<span class="company-initial">{{ company.name[:1]|upper }}</span>{% endif %}
      <span>{{ company.name }}{% if support_mode %}<small>Mode support</small>{% endif %}</span>
    </div>
    {% endif %}
    {% if in_platform %}
    <nav>
      <p class="nav-title">Plateforme</p>
      <a href="{{ url_for('platform.index') }}" class="{{ 'active' if ep in ('platform.index','platform.company_detail','platform.company_edit') }}"><i>🏢</i>Entreprises clientes</a>
      <a href="{{ url_for('platform.company_new') }}" class="nav-cta {{ 'active' if ep == 'platform.company_new' }}"><i>＋</i>Nouvelle entreprise</a>
      <p class="nav-title">Mon compte</p>
      <a href="{{ url_for('auth.profile') }}" class="{{ 'active' if ep == 'auth.profile' }}"><i>👤</i>Mon profil</a>
      <a href="{{ url_for('auth.change_password') }}" class="{{ 'active' if ep == 'auth.change_password' }}"><i>🔑</i>Mot de passe</a>
    </nav>
    {% else %}
    <nav>
      <a href="{{ url_for('dashboard.index') }}" class="{{ 'active' if ep.startswith('dashboard') }}"><i>▦</i>Tableau de bord</a>
      {% if current_user.effective_role in ('admin','gestionnaire','vendeur') %}
      <a href="{{ url_for('sales.create') }}" class="nav-cta {{ 'active' if ep == 'sales.create' }}"><i>＋</i>Nouvelle vente</a>
      {% endif %}
      <a href="{{ url_for('proinvoices.index') }}" class="{{ 'active' if ep.startswith('proinvoices.') }}"><i>📄</i>Facture Pro</a>
      <p class="nav-title">Commerce</p>
      <a href="{{ url_for('sales.index') }}" class="{{ 'active' if ep.startswith('sales.') and 'customer' not in ep and ep != 'sales.create' }}"><i>🧾</i>Ventes &amp; factures</a>
      <a href="{{ url_for('sales.customers') }}" class="{{ 'active' if 'customer' in ep }}"><i>👥</i>Clients</a>
      <a href="{{ url_for('purchases.index') }}" class="{{ 'active' if ep.startswith('purchases.') and 'supplier' not in ep }}"><i>🛒</i>Achats</a>
      <a href="{{ url_for('purchases.suppliers') }}" class="{{ 'active' if 'supplier' in ep }}"><i>🚚</i>Fournisseurs</a>
      <p class="nav-title">Inventaire</p>
      <a href="{{ url_for('products.index') }}" class="{{ 'active' if ep.startswith('products.') }}"><i>📦</i>Produits</a>
      <a href="{{ url_for('stock.index') }}" class="{{ 'active' if ep.startswith('stock.') }}"><i>⇅</i>Stocks{% if low_stock_count %}<span class="badge badge-danger ml">{{ low_stock_count }}</span>{% endif %}</a>
      {% if current_user.effective_role in ('admin','gestionnaire') %}
      <a href="{{ url_for('analytics.index') }}" class="{{ 'active' if ep.startswith('analytics.') }}"><i>📈</i>Analyses</a>
      {% endif %}
      <p class="nav-title">Bureau</p>
      <a href="{{ url_for('archives.documents') }}" class="{{ 'active' if ep in ('archives.documents',) }}"><i>🗂</i>Archives</a>
      <a href="{{ url_for('archives.messages') }}" class="{{ 'active' if 'message' in ep or ep == 'archives.compose' }}"><i>✉</i>Messages{% if unread_count %}<span class="badge badge-danger ml">{{ unread_count }}</span>{% endif %}</a>
      {% if current_user.effective_role == 'admin' %}
      <p class="nav-title">Administration</p>
      <a href="{{ url_for('auth.users') }}" class="{{ 'active' if ep.startswith('auth.user') }}"><i>🔐</i>Utilisateurs</a>
      <a href="{{ url_for('settings.index') }}" class="{{ 'active' if ep.startswith('settings.') }}"><i>⚙</i>Paramètres</a>
      {% endif %}
    </nav>
    {% endif %}
    <div class="sidebar-user">
      <a href="{{ url_for('auth.profile') }}" class="avatar">{{ current_user.full_name[:1]|upper }}</a>
      <div class="who"><a href="{{ url_for('auth.profile') }}">{{ current_user.full_name }}</a><small>{{ current_user.role_label }}</small></div>
      <form method="post" action="{{ url_for('auth.logout') }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <button class="icon-btn" title="Déconnexion" aria-label="Déconnexion">⏻</button>
      </form>
    </div>
  </aside>
  <div class="overlay" id="overlay"></div>

  <main class="main">
    {% if support_mode %}
    <div class="support-banner">
      <span>🛠 Mode support : vous agissez dans l'espace de <strong>{{ company.name }}</strong>.</span>
      <form method="post" action="{{ url_for('platform.exit_support') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm btn-light" type="submit">Quitter et revenir à la plateforme</button></form>
    </div>
    {% endif %}
    {% include "_flash.html" %}
    {% block content %}{% endblock %}
    <footer class="footer">© {{ now.year }} {% if company %}{{ company.name }} · Propulsé par {% endif %}ANSARTECH 224</footer>
  </main>
{% else %}
  <main class="auth-main">
    {% include "_flash.html" %}
    {% block auth_content %}{% endblock %}
  </main>
{% endif %}
<script src="{{ url_for('static', filename='js/app.js') }}"></script>
{% block scripts %}{% endblock %}
</body>
</html>
''',

# ---------------------------------------------------------------- dashboard.html
'dashboard.html': r'''{% extends "base.html" %}
{% from "_macros.html" import sale_badge, stock_badge, empty with context %}
{% block title %}Tableau de bord{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Bonjour, {{ current_user.full_name.split(' ')[0] }}</h1><p>Voici l'activité de {{ settings.company_name }} aujourd'hui.</p></div>
  <div class="actions">
    <a class="btn btn-accent" href="{{ url_for('sales.create') }}">＋ Nouvelle vente</a>
    <a class="btn btn-light" href="{{ url_for('proinvoices.edit') }}">📄 Facture Pro</a>
    {% if current_user.effective_role in ('admin','gestionnaire') %}<a class="btn btn-light" href="{{ url_for('purchases.create') }}">Nouvel achat</a>{% endif %}
  </div>
</div>

<div class="kpis">
  <div class="kpi"><div class="label">Ventes du jour</div><div class="value">{{ stats.today_total|money }}</div><div class="sub">{{ stats.today_count }} vente(s)</div></div>
  <div class="kpi k-accent"><div class="label">Ventes du mois</div><div class="value">{{ stats.month_total|money }}</div>
    <div class="sub">{% if current_user.effective_role in ('admin','gestionnaire') %}Bénéfice : {{ stats.month_profit|money }}{% else %}Mois en cours{% endif %}</div></div>
  <div class="kpi k-blue"><div class="label">Valeur du stock</div><div class="value">{{ stats.stock_value|money }}</div><div class="sub">{{ stats.product_count }} produit(s) actif(s)</div></div>
  <div class="kpi k-red"><div class="label">Créances clients</div><div class="value">{{ stats.receivables|money }}</div><div class="sub">{{ stats.pending_purchases }} achat(s) en attente</div></div>
</div>

<div class="grid grid-main">
  <div class="card">
    <div class="card-head"><h2>Ventes des 14 derniers jours</h2><a href="{{ url_for('sales.index') }}">Toutes les ventes →</a></div>
    <div class="chart-box"><canvas id="salesChart" aria-label="Graphique des ventes"></canvas></div>
  </div>
  <div class="card">
    <div class="card-head"><h2>Alertes de stock</h2><a href="{{ url_for('stock.index') }}">Voir →</a></div>
    {% if low_stock %}
    <ul class="list-plain">
      {% for p in low_stock %}
      <li><span><a href="{{ url_for('products.detail', product_id=p.id) }}">{{ p.name }}</a><span class="cell-sub">{{ p.quantity|num }} / min {{ p.min_stock|num }} {{ p.unit }}</span></span>{{ stock_badge(p) }}</li>
      {% endfor %}
    </ul>
    {% else %}{{ empty("Aucune alerte : tous les stocks sont suffisants.") }}{% endif %}
  </div>
</div>

<div class="card flush">
  <div class="card-head"><h2>Dernières ventes</h2></div>
  {% if recent_sales %}
  <div class="table-wrap"><table>
    <thead><tr><th>Facture</th><th class="hide-sm">Date</th><th>Client</th><th class="num">Total</th><th>Statut</th></tr></thead>
    <tbody>{% for s in recent_sales %}
      <tr><td><a href="{{ url_for('sales.detail', sale_id=s.id) }}">{{ s.invoice_number }}</a></td>
        <td class="hide-sm nowrap">{{ s.created_at|datetime }}</td>
        <td>{{ s.customer.name if s.customer else 'Client comptant' }}</td>
        <td class="num">{{ s.total|money }}</td><td>{{ sale_badge(s.status, s.status_label) }}</td></tr>
    {% endfor %}</tbody>
  </table></div>
  {% else %}{{ empty("Aucune vente pour le moment.") }}{% endif %}
</div>
{% endblock %}
{% block scripts %}
<script src="{{ url_for('static', filename='js/chart.umd.min.js') }}"></script>
<script>
(function () {
  if (!window.Chart) return;
  new Chart(document.getElementById("salesChart"), {
    type: "bar",
    data: { labels: {{ chart_labels|tojson }}, datasets: [{ label: "Ventes", data: {{ chart_values|tojson }}, backgroundColor: "#0b5d43", borderRadius: 6, maxBarThickness: 34 }] },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false },
      tooltip: { callbacks: { label: function (c) { return ansarFmt(c.parsed.y) + " {{ settings.currency }}"; } } } },
      scales: { y: { beginAtZero: true, ticks: { callback: function (v) { return v >= 1e6 ? (v / 1e6) + " M" : (v >= 1e3 ? (v / 1e3) + " k" : v); } } }, x: { grid: { display: false } } } }
  });
})();
</script>
{% endblock %}
''',

# ---------------------------------------------------------------- errors/error.html
'errors/error.html': r'''{% extends "base.html" %}
{% block title %}Erreur {{ code }}{% endblock %}
{% block content %}
<div class="error-page card">
  <div class="code">{{ code }}</div>
  <p>{{ message }}</p>
  <a class="btn" href="{{ url_for('dashboard.index') }}">Retour au tableau de bord</a>
</div>
{% endblock %}
{% block auth_content %}
<div class="auth-card error-page">
  <div class="code">{{ code }}</div>
  <p>{{ message }}</p>
  <a class="btn" href="{{ url_for('auth.login') }}">Aller à la connexion</a>
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- platform/company_detail.html
'platform/company_detail.html': r'''{% extends "base.html" %}
{% block title %}{{ company.name }}{% endblock %}
{% block content %}
<div class="page-head">
  <div style="display:flex;gap:.9rem;align-items:center">
    {% if company.logo %}<img class="logo-preview" style="width:64px;height:64px" src="{{ url_for('platform.company_logo', company_id=company.id) }}" alt="">{% endif %}
    <div><h1>{{ company.name }}
      {% if not company.is_active %}<span class="badge badge-muted">Suspendue</span>{% elif company.is_expired %}<span class="badge badge-danger">Expirée</span>{% else %}<span class="badge badge-success">Active</span>{% endif %}</h1>
      <p>{{ company.code }}{% if company.activity %} · {{ company.activity }}{% endif %} · cliente depuis le {{ company.created_at|date }}</p></div>
  </div>
  <div class="actions">
    <a class="btn btn-light" href="{{ url_for('platform.company_edit', company_id=company.id) }}">Modifier</a>
    <form method="post" action="{{ url_for('platform.enter_support', company_id=company.id) }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <button class="btn" type="submit">Ouvrir son espace (support)</button>
    </form>
  </div>
</div>

<div class="kpis">
  <div class="kpi"><div class="label">Utilisateurs</div><div class="value">{{ stats.users }} / {{ company.max_users }}</div></div>
  <div class="kpi k-accent"><div class="label">Ventes (30 j)</div><div class="value">{{ stats.sales_30 }}</div><div class="sub">{{ stats.revenue_30|money('GNF') }}</div></div>
  <div class="kpi k-blue"><div class="label">Produits</div><div class="value">{{ stats.products }}</div></div>
  <div class="kpi k-red"><div class="label">Dernière activité</div><div class="value" style="font-size:1rem">{{ stats.last_activity|datetime or '—' }}</div></div>
</div>

<div class="grid grid-main">
  <div>
    <div class="card">
      <h2>Fiche entreprise</h2>
      <dl class="dl">
        <dt>Adresse</dt><dd>{{ company.full_address or '—' }}</dd>
        <dt>Téléphone</dt><dd>{{ company.phone or '—' }}</dd>
        <dt>E-mail</dt><dd>{{ company.email or '—' }}</dd>
        <dt>Site web</dt><dd>{{ company.website or '—' }}</dd>
        <dt>Contact</dt><dd>{{ company.contact_name or '—' }}</dd>
        <dt>RCCM / NIF</dt><dd>{{ company.rccm or '—' }} / {{ company.nif or '—' }}</dd>
      </dl>
      {% if company.notes %}<p class="muted" style="white-space:pre-wrap;margin-top:1rem">{{ company.notes }}</p>{% endif %}
    </div>
    <div class="card flush">
      <div class="card-head"><h2>Utilisateurs</h2></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Nom</th><th>Rôle</th><th class="hide-sm">Dernière connexion</th><th>Nouveau mot de passe</th></tr></thead>
        <tbody>{% for u in users %}
          <tr class="{{ 'row-muted' if not u.is_active_account }}"><td><span class="cell-title">{{ u.full_name }}</span><span class="cell-sub">{{ u.username }}</span></td>
            <td>{{ u.role_label }}</td><td class="hide-sm">{{ u.last_login|datetime or 'Jamais' }}</td>
            <td><form method="post" action="{{ url_for('platform.reset_user_password', company_id=company.id, user_id=u.id) }}" class="swatch-row" data-confirm="Réinitialiser le mot de passe de {{ u.username }} ?">
              <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
              <input type="text" name="password" minlength="8" required placeholder="Provisoire" aria-label="Nouveau mot de passe" autocomplete="off" style="min-width:120px">
              <button class="btn btn-light btn-sm" type="submit">OK</button></form></td></tr>
        {% endfor %}</tbody>
      </table></div>
    </div>
  </div>
  <div>
    <div class="card">
      <h2>Abonnement</h2>
      <p>Fin : <strong>{{ company.subscription_end.strftime('%d/%m/%Y') if company.subscription_end else 'illimité' }}</strong></p>
      <form method="post" action="{{ url_for('platform.company_extend', company_id=company.id) }}" class="swatch-row">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <select name="months" aria-label="Durée"><option value="1">+ 1 mois</option><option value="3">+ 3 mois</option><option value="6">+ 6 mois</option><option value="12">+ 12 mois</option></select>
        <button class="btn" type="submit">Prolonger</button>
      </form>
      <form method="post" action="{{ url_for('platform.company_toggle', company_id=company.id) }}" style="margin-top:1rem"
            data-confirm="{{ 'Suspendre cette entreprise ? Ses utilisateurs ne pourront plus se connecter.' if company.is_active else 'Réactiver cette entreprise ?' }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <button class="btn btn-block {{ 'btn-ghost-danger' if company.is_active else 'btn-light' }}" type="submit">{{ "Suspendre l'entreprise" if company.is_active else "Réactiver l'entreprise" }}</button>
      </form>
    </div>
    <div class="card">
      <h2>Activité récente</h2>
      {% if logs %}<ul class="list-plain">{% for l in logs %}<li><span>{{ l.action|replace('_',' ') }}<span class="cell-sub">{{ l.details }}</span></span><small class="nowrap">{{ l.created_at|datetime }}</small></li>{% endfor %}</ul>
      {% else %}<p class="muted">Aucune activité.</p>{% endif %}
    </div>
  </div>
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- platform/company_form.html
'platform/company_form.html': r'''{% extends "base.html" %}
{% block title %}{{ 'Modifier' if company else 'Nouvelle' }} entreprise{% endblock %}
{% block content %}
{% set v = form if form else {} %}
<div class="page-head"><div><h1>{{ 'Modifier ' ~ company.name if company else 'Nouvelle entreprise cliente' }}</h1>
  <p>{{ "Fiche, adresse et abonnement de l'entreprise." if company else "L'entreprise reçoit son propre espace isolé, avec son administrateur." }}</p></div></div>
<form method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <div class="card">
    <h2>Identité</h2>
    <div class="form-grid">
      <div class="field"><label for="name">Nom de l'entreprise <span class="req">*</span></label><input type="text" id="name" name="name" value="{{ v.get('name','') }}" required maxlength="150"></div>
      <div class="field"><label for="activity">Activité</label><input type="text" id="activity" name="activity" value="{{ v.get('activity','') }}" placeholder="ex. Quincaillerie, pharmacie…"></div>
      <div class="field"><label for="code">Code entreprise</label><input type="text" id="code" name="code" value="{{ v.get('code','') }}" pattern="[a-z0-9\-]{2,30}" placeholder="Laissez vide : généré depuis le nom">
        <div class="hint">Identifiant interne unique (minuscules, chiffres, tirets).</div></div>
      <div class="field"><label for="contact_name">Personne de contact</label><input type="text" id="contact_name" name="contact_name" value="{{ v.get('contact_name','') }}"></div>
      <div class="field"><label for="rccm">RCCM</label><input type="text" id="rccm" name="rccm" value="{{ v.get('rccm','') }}"></div>
      <div class="field"><label for="nif">NIF</label><input type="text" id="nif" name="nif" value="{{ v.get('nif','') }}"></div>
    </div>
  </div>
  <div class="card">
    <h2>Adresse et coordonnées</h2>
    <div class="form-grid">
      <div class="field full"><label for="address">Adresse</label><input type="text" id="address" name="address" value="{{ v.get('address','') }}" placeholder="Quartier, rue, immeuble…"></div>
      <div class="field"><label for="city">Ville</label><input type="text" id="city" name="city" value="{{ v.get('city','') }}"></div>
      <div class="field"><label for="country">Pays</label><input type="text" id="country" name="country" value="{{ v.get('country','') }}"></div>
      <div class="field"><label for="phone">Téléphone</label><input type="tel" id="phone" name="phone" value="{{ v.get('phone','') }}" placeholder="+224 6xx xx xx xx"></div>
      <div class="field"><label for="email">E-mail</label><input type="email" id="email" name="email" value="{{ v.get('email','') }}"></div>
      <div class="field"><label for="website">Site web</label><input type="text" id="website" name="website" value="{{ v.get('website','') }}"></div>
    </div>
  </div>
  <div class="card">
    <h2>Abonnement</h2>
    <div class="form-grid">
      <div class="field"><label for="subscription_end">Fin d'abonnement</label><input type="date" id="subscription_end" name="subscription_end" value="{{ v.get('subscription_end','') }}">
        <div class="hint">Vide = sans limite. Après cette date, les utilisateurs ne peuvent plus se connecter.</div></div>
      <div class="field"><label for="max_users">Utilisateurs autorisés <span class="req">*</span></label><input type="number" id="max_users" name="max_users" min="1" max="500" value="{{ v.get('max_users','5') }}" required></div>
      <div class="field full"><label for="notes">Notes internes</label><textarea id="notes" name="notes" placeholder="Tarif, mode de paiement de l'abonnement…">{{ v.get('notes','') }}</textarea></div>
    </div>
  </div>
  {% if not company %}
  <div class="card">
    <h2>Administrateur de l'entreprise</h2>
    <p class="muted">Ce compte gérera l'entreprise (produits, utilisateurs, paramètres). Il devra changer son mot de passe à la première connexion.</p>
    <div class="form-grid">
      <div class="field"><label for="admin_full_name">Nom complet</label><input type="text" id="admin_full_name" name="admin_full_name" value="{{ v.get('admin_full_name','') }}"></div>
      <div class="field"><label for="admin_username">Identifiant <span class="req">*</span></label><input type="text" id="admin_username" name="admin_username" value="{{ v.get('admin_username','') }}" autocapitalize="none" required placeholder="ex. kaloum.admin">
        <div class="hint">Unique sur toute la plateforme.</div></div>
      <div class="field"><label for="admin_password">Mot de passe provisoire <span class="req">*</span></label><input type="text" id="admin_password" name="admin_password" required minlength="8" autocomplete="off">
        <div class="hint">8 caractères min., lettres et chiffres. Communiquez-le au client.</div></div>
    </div>
  </div>
  {% endif %}
  <div class="form-actions">
    <a class="btn btn-light" href="{{ url_for('platform.company_detail', company_id=company.id) if company else url_for('platform.index') }}">Annuler</a>
    <button class="btn" type="submit">{{ 'Enregistrer' if company else "Créer l'entreprise" }}</button>
  </div>
</form>
{% endblock %}
''',

# ---------------------------------------------------------------- platform/index.html
'platform/index.html': r'''{% extends "base.html" %}
{% from "_macros.html" import empty with context %}
{% block title %}Plateforme{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Entreprises clientes</h1><p>Gérez les entreprises qui utilisent ANSARTECH 224, leurs abonnements et leurs accès.</p></div>
  <div class="actions"><a class="btn btn-accent" href="{{ url_for('platform.company_new') }}">＋ Nouvelle entreprise</a></div>
</div>

<div class="kpis">
  <div class="kpi"><div class="label">Entreprises</div><div class="value">{{ totals.companies }}</div><div class="sub">au total</div></div>
  <div class="kpi k-accent"><div class="label">Actives</div><div class="value">{{ totals.active }}</div><div class="sub">peuvent se connecter</div></div>
  <div class="kpi k-red"><div class="label">Expirent bientôt</div><div class="value">{{ totals.expiring }}</div><div class="sub">dans les 15 jours</div></div>
  <div class="kpi k-blue"><div class="label">Utilisateurs</div><div class="value">{{ totals.users }}</div><div class="sub">toutes entreprises</div></div>
</div>

<form class="filters" method="get">
  <input type="search" name="q" value="{{ q }}" placeholder="Nom, code, téléphone, ville…" aria-label="Rechercher">
  <select name="statut" aria-label="Statut">
    <option value="">Toutes</option>
    <option value="actives" {{ 'selected' if status == 'actives' }}>Actives</option>
    <option value="bientot" {{ 'selected' if status == 'bientot' }}>Expirent bientôt</option>
    <option value="expirees" {{ 'selected' if status == 'expirees' }}>Expirées</option>
    <option value="suspendues" {{ 'selected' if status == 'suspendues' }}>Suspendues</option>
  </select>
  <button class="btn" type="submit">Filtrer</button>
</form>

<div class="card flush">
  {% if companies %}
  <div class="table-wrap"><table>
    <thead><tr><th>Entreprise</th><th class="hide-sm">Ville</th><th class="num hide-sm">Utilisateurs</th><th class="num hide-sm">Ventes 30 j</th><th>Abonnement</th><th>Statut</th></tr></thead>
    <tbody>{% for c in companies %}{% set st = stats[c.id] %}
      <tr class="{{ 'row-muted' if not c.can_login }}">
        <td><a class="cell-title" href="{{ url_for('platform.company_detail', company_id=c.id) }}">{{ c.name }}</a><span class="cell-sub">{{ c.code }}{% if c.phone %} · {{ c.phone }}{% endif %}</span></td>
        <td class="hide-sm">{{ c.city or '—' }}</td>
        <td class="num hide-sm">{{ st.users }} / {{ c.max_users }}</td>
        <td class="num hide-sm">{{ st.sales_30 }}<span class="cell-sub">{{ st.revenue_30|money('GNF') }}</span></td>
        <td class="nowrap">{% if c.subscription_end %}{{ c.subscription_end.strftime('%d/%m/%Y') }}{% if not c.is_expired and (c.subscription_end - today).days <= 15 %}<span class="cell-sub" style="color:#b86e00">dans {{ (c.subscription_end - today).days }} j</span>{% endif %}{% else %}Illimité{% endif %}</td>
        <td>{% if not c.is_active %}<span class="badge badge-muted">Suspendue</span>{% elif c.is_expired %}<span class="badge badge-danger">Expirée</span>{% else %}<span class="badge badge-success">Active</span>{% endif %}</td>
      </tr>
    {% endfor %}</tbody>
  </table></div>
  {% else %}{{ empty("Aucune entreprise. Créez votre première entreprise cliente.") }}{% endif %}
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- products/categories.html
'products/categories.html': r'''{% extends "base.html" %}
{% block title %}Catégories{% endblock %}
{% block content %}
<div class="page-head"><div><h1>Catégories de produits</h1></div>
  <div class="actions"><a class="btn btn-light" href="{{ url_for('products.index') }}">← Produits</a></div></div>
<div class="grid grid-2">
  <div class="card">
    <h2>Ajouter une catégorie</h2>
    <form method="post" class="filters" style="margin:0">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <input type="text" name="name" placeholder="Nom de la catégorie" maxlength="80" required aria-label="Nom">
      <button class="btn" type="submit">Ajouter</button>
    </form>
  </div>
  <div class="card">
    <h2>Catégories existantes</h2>
    <ul class="list-plain">
      {% for c in categories %}
      <li><span>{{ c.name }} <small>({{ c.products|length }} produit(s))</small></span>
        {% if not c.products %}
        <form method="post" action="{{ url_for('products.delete_category', category_id=c.id) }}" data-confirm="Supprimer la catégorie {{ c.name }} ?">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
          <button class="btn btn-ghost-danger btn-sm" type="submit">Supprimer</button>
        </form>{% endif %}
      </li>
      {% else %}<li class="muted">Aucune catégorie.</li>{% endfor %}
    </ul>
  </div>
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- products/detail.html
'products/detail.html': r'''{% extends "base.html" %}
{% from "_macros.html" import stock_badge, empty with context %}
{% block title %}{{ product.name }}{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>{{ product.name }}</h1><p>{{ product.sku }}{% if product.category %} · {{ product.category.name }}{% endif %} {% if not product.is_active %}<span class="badge badge-muted">Archivé</span>{% endif %}</p></div>
  {% if current_user.effective_role in ('admin','gestionnaire') %}
  <div class="actions">
    <a class="btn btn-light" href="{{ url_for('stock.movement', produit=product.id) }}">Mouvement de stock</a>
    <a class="btn" href="{{ url_for('products.form', product_id=product.id) }}">Modifier</a>
  </div>
  {% endif %}
</div>

<div class="kpis">
  <div class="kpi"><div class="label">En stock</div><div class="value">{{ product.quantity|num }} {{ product.unit }}</div><div class="sub">{{ stock_badge(product) }} min {{ product.min_stock|num }}</div></div>
  <div class="kpi k-accent"><div class="label">Prix de vente</div><div class="value">{{ product.sale_price|money }}</div><div class="sub">Achat : {{ product.purchase_price|money }}</div></div>
  <div class="kpi k-blue"><div class="label">Marge unitaire</div><div class="value">{{ product.margin|money }}</div><div class="sub">{{ '%.1f'|format(product.margin_rate) }} % du prix de vente</div></div>
  <div class="kpi k-red"><div class="label">Valeur du stock</div><div class="value">{{ product.stock_value|money }}</div><div class="sub">au prix d'achat</div></div>
</div>

<div class="grid grid-main">
  <div class="card flush">
    <div class="card-head"><h2>Derniers mouvements</h2><a href="{{ url_for('stock.index', produit=product.id, jours=365) }}">Historique →</a></div>
    {% if movements %}
    <div class="table-wrap"><table>
      <thead><tr><th>Date</th><th>Type</th><th class="num">Qté</th><th class="num">Stock</th><th class="hide-sm">Motif</th></tr></thead>
      <tbody>{% for m in movements %}
        <tr><td class="nowrap">{{ m.created_at|datetime }}</td><td>{{ m.type_label }}</td>
          <td class="num" style="color:{{ '#0b5d43' if m.quantity > 0 else '#c62f2f' }}">{{ '+' if m.quantity > 0 }}{{ m.quantity|num }}</td>
          <td class="num">{{ m.stock_after|num }}</td><td class="hide-sm">{{ m.reason }} {% if m.reference %}<span class="cell-sub">{{ m.reference }}</span>{% endif %}</td></tr>
      {% endfor %}</tbody>
    </table></div>
    {% else %}{{ empty("Aucun mouvement.") }}{% endif %}
  </div>
  <div class="card">
    <h2>Informations</h2>
    <dl class="dl">
      <dt>Code-barres</dt><dd>{{ product.barcode or '—' }}</dd>
      <dt>Unité</dt><dd>{{ product.unit }}</dd>
      <dt>Créé le</dt><dd>{{ product.created_at|date }}</dd>
      <dt>Mis à jour</dt><dd>{{ product.updated_at|datetime }}</dd>
    </dl>
    {% if product.description %}<p style="margin-top:1rem">{{ product.description }}</p>{% endif %}
    {% if current_user.effective_role in ('admin','gestionnaire') %}
    <div class="actions" style="margin-top:1.25rem">
      <form method="post" action="{{ url_for('products.toggle_archive', product_id=product.id) }}" data-confirm="{{ 'Archiver ce produit ? Il ne sera plus proposé à la vente.' if product.is_active else 'Réactiver ce produit ?' }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <button class="btn btn-light btn-sm" type="submit">{{ 'Archiver' if product.is_active else 'Réactiver' }}</button>
      </form>
      {% if current_user.effective_role == 'admin' %}
      <form method="post" action="{{ url_for('products.delete', product_id=product.id) }}" data-confirm="Supprimer définitivement ce produit ?">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <button class="btn btn-ghost-danger btn-sm" type="submit">Supprimer</button>
      </form>
      {% endif %}
    </div>
    {% endif %}
  </div>
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- products/form.html
'products/form.html': r'''{% extends "base.html" %}
{% block title %}{{ 'Modifier' if product else 'Nouveau' }} produit{% endblock %}
{% block content %}
{% set v = form if form else {} %}
<div class="page-head"><div><h1>{{ 'Modifier : ' ~ product.name if product else 'Nouveau produit' }}</h1></div></div>
<div class="card" style="max-width:860px">
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="form-grid">
      <div class="field"><label for="sku">Code produit <span class="req">*</span></label>
        <input type="text" id="sku" name="sku" value="{{ v.get('sku', product.sku if product else '') }}" maxlength="40" required placeholder="ex. TEL-001"></div>
      <div class="field"><label for="barcode">Code-barres</label>
        <input type="text" id="barcode" name="barcode" value="{{ v.get('barcode', (product.barcode or '') if product else '') }}" inputmode="numeric"></div>
      <div class="field full"><label for="name">Nom du produit <span class="req">*</span></label>
        <input type="text" id="name" name="name" value="{{ v.get('name', product.name if product else '') }}" maxlength="150" required></div>
      <div class="field"><label for="category_id">Catégorie</label>
        <select id="category_id" name="category_id">
          <option value="">— Aucune —</option>
          {% set cur = v.get('category_id', product.category_id if product else '')|string %}
          {% for c in categories %}<option value="{{ c.id }}" {{ 'selected' if c.id|string == cur }}>{{ c.name }}</option>{% endfor %}
        </select></div>
      <div class="field"><label for="unit">Unité</label>
        <input type="text" id="unit" name="unit" value="{{ v.get('unit', product.unit if product else 'pièce') }}" list="units">
        <datalist id="units"><option value="pièce"><option value="carton"><option value="kg"><option value="litre"><option value="sac"><option value="paquet"><option value="mètre"></datalist></div>
      <div class="field"><label for="purchase_price">Prix d'achat ({{ settings.currency }})</label>
        <input type="number" id="purchase_price" name="purchase_price" min="0" step="any" inputmode="decimal" value="{{ v.get('purchase_price', ('%g' % product.purchase_price) if product else '0') }}"></div>
      <div class="field"><label for="sale_price">Prix de vente ({{ settings.currency }})</label>
        <input type="number" id="sale_price" name="sale_price" min="0" step="any" inputmode="decimal" value="{{ v.get('sale_price', ('%g' % product.sale_price) if product else '0') }}"></div>
      {% if not product %}
      <div class="field"><label for="quantity">Stock initial</label>
        <input type="number" id="quantity" name="quantity" min="0" step="any" inputmode="decimal" value="{{ v.get('quantity', '0') }}"></div>
      {% endif %}
      <div class="field"><label for="min_stock">Stock minimum (alerte)</label>
        <input type="number" id="min_stock" name="min_stock" min="0" step="any" inputmode="decimal" value="{{ v.get('min_stock', ('%g' % product.min_stock) if product else '5') }}"></div>
      <div class="field full"><label for="description">Description</label>
        <textarea id="description" name="description">{{ v.get('description', (product.description or '') if product else '') }}</textarea></div>
    </div>
    {% if product %}<p class="muted">Pour modifier la quantité en stock, utilisez un <a href="{{ url_for('stock.movement', produit=product.id) }}">mouvement de stock</a> (traçabilité).</p>{% endif %}
    <div class="form-actions">
      <a class="btn btn-light" href="{{ url_for('products.detail', product_id=product.id) if product else url_for('products.index') }}">Annuler</a>
      <button class="btn" type="submit">Enregistrer</button>
    </div>
  </form>
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- products/index.html
'products/index.html': r'''{% extends "base.html" %}
{% from "_macros.html" import pager, stock_badge, empty with context %}
{% block title %}Produits{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Produits</h1><p>{{ pagination.total }} produit(s){{ ' archivé(s)' if status == 'archives' }}</p></div>
  <div class="actions">
    <a class="btn btn-light" href="{{ url_for('products.export') }}">Exporter (Excel)</a>
    {% if current_user.effective_role in ('admin','gestionnaire') %}
    <a class="btn btn-light" href="{{ url_for('products.categories') }}">Catégories</a>
    <a class="btn" href="{{ url_for('products.form') }}">＋ Nouveau produit</a>
    {% endif %}
  </div>
</div>

<form class="filters" method="get">
  <input type="search" name="q" value="{{ q }}" placeholder="Nom, code ou code-barres…" aria-label="Rechercher">
  <select name="categorie" aria-label="Catégorie">
    <option value="">Toutes les catégories</option>
    {% for c in categories %}<option value="{{ c.id }}" {{ 'selected' if c.id == category_id }}>{{ c.name }}</option>{% endfor %}
  </select>
  <select name="statut" aria-label="Statut">
    <option value="">Tous les stocks</option>
    <option value="bas" {{ 'selected' if status == 'bas' }}>Stock bas</option>
    <option value="rupture" {{ 'selected' if status == 'rupture' }}>En rupture</option>
    <option value="archives" {{ 'selected' if status == 'archives' }}>Produits archivés</option>
  </select>
  <button class="btn" type="submit">Filtrer</button>
</form>

<div class="card flush">
  {% if products %}
  <div class="table-wrap"><table>
    <thead><tr><th>Produit</th><th class="hide-sm">Catégorie</th><th class="num hide-sm">Prix achat</th><th class="num">Prix vente</th><th class="num">Stock</th><th>État</th></tr></thead>
    <tbody>{% for p in products %}
      <tr>
        <td><a class="cell-title" href="{{ url_for('products.detail', product_id=p.id) }}">{{ p.name }}</a><span class="cell-sub">{{ p.sku }}</span></td>
        <td class="hide-sm">{{ p.category.name if p.category else '—' }}</td>
        <td class="num hide-sm">{{ p.purchase_price|money }}</td>
        <td class="num">{{ p.sale_price|money }}</td>
        <td class="num">{{ p.quantity|num }} <small>{{ p.unit }}</small></td>
        <td>{{ stock_badge(p) }}</td>
      </tr>
    {% endfor %}</tbody>
  </table></div>
  {{ pager(pagination, 'products.index') }}
  {% else %}{{ empty("Aucun produit trouvé.") }}{% endif %}
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- proinvoices/detail.html
'proinvoices/detail.html': r'''{% extends "base.html" %}
{% block title %}{{ invoice.number }}{% endblock %}
{% block head %}
{% set brand = settings.invoice_color if settings.invoice_color and settings.invoice_color|length == 7 else '#0b5d43' %}
<style>
  .pv { --brand: {{ brand }}; }
</style>
{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>{{ invoice.title|capitalize }} {{ invoice.number }}</h1>
    <p>{{ invoice.client_name }} · {{ invoice.issue_date.strftime('%d/%m/%Y') }} · <strong>{{ invoice.total|money }}</strong></p></div>
  <div class="actions">
    <a class="btn btn-light" href="{{ url_for('proinvoices.edit', invoice_id=invoice.id) }}">✏ Modifier</a>
    <form class="inline" method="post" action="{{ url_for('proinvoices.duplicate', invoice_id=invoice.id) }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <button class="btn btn-light" type="submit">⧉ Dupliquer</button>
    </form>
  </div>
</div>

<div class="card pro-actions {{ 'pulse' if focus_whatsapp }}" id="shareBar">
  <a class="btn" id="pdfDownload" href="{{ url_for('proinvoices.pdf', invoice_id=invoice.id) }}" download="{{ invoice.number }}.pdf">📄 Télécharger le PDF</a>
  <a class="btn btn-light" href="{{ url_for('proinvoices.pdf', invoice_id=invoice.id, voir=1) }}" target="_blank" rel="noopener">👁 Ouvrir le PDF</a>
  <button class="btn btn-whatsapp" type="button" id="waShare">🟢 Envoyer le PDF par WhatsApp</button>
  <a class="btn btn-light" href="{{ wa_url }}" target="_blank" rel="noopener" id="waLink">💬 Envoyer le lien WhatsApp</a>
</div>
{% if focus_whatsapp %}<p class="muted" style="margin:-.5rem 0 1rem">Appuyez sur un bouton vert ou « Envoyer le lien » pour ouvrir WhatsApp{% if invoice.client_phone %} vers {{ invoice.client_phone }}{% endif %}.</p>{% endif %}

<div class="pv">
  <div class="pv-head">
    {% if settings.show_logo_on_invoice == '1' and company and company.logo %}<img src="{{ url_for('settings.logo', v=company.id) }}" alt="">{% endif %}
    <div class="pv-company">{{ settings.company_name }}</div>
    {% if settings.company_activity %}<div class="pv-activity">{{ settings.company_activity }}</div>{% endif %}
    {% if settings.invoice_address_display %}<div class="pv-small">{{ settings.invoice_address_display.splitlines()|select|join(' · ') }}</div>{% endif %}
    <div class="pv-small">{% if settings.invoice_phone_display %}Tél : {{ settings.invoice_phone_display }}{% endif %}{% if settings.invoice_email_display %} · {{ settings.invoice_email_display }}{% endif %}{% if settings.company_website %} · {{ settings.company_website }}{% endif %}</div>
    {% if settings.company_rccm or settings.company_nif %}<div class="pv-small">{% if settings.company_rccm %}RCCM : {{ settings.company_rccm }}{% endif %}{% if settings.company_rccm and settings.company_nif %} · {% endif %}{% if settings.company_nif %}NIF : {{ settings.company_nif }}{% endif %}</div>{% endif %}
  </div>
  <div class="pv-rule"></div>
  <div class="pv-title">{{ invoice.title }}</div>
  <div class="pv-number">N° {{ invoice.number }}</div>

  <div class="pv-boxes">
    <div class="pv-box">
      <small>{{ 'CLIENT' if invoice.doc_type == 'devis' else 'FACTURÉ À' }}</small>
      <strong>{{ invoice.client_name }}</strong>
      {% if invoice.client_address %}<span class="pv-pre">{{ invoice.client_address }}</span>{% endif %}
      {% if invoice.client_phone %}<span>Tél : {{ invoice.client_phone }}</span>{% endif %}
      {% if invoice.client_email %}<span>{{ invoice.client_email }}</span>{% endif %}
      {% if invoice.client_nif %}<span>NIF : {{ invoice.client_nif }}</span>{% endif %}
    </div>
    <div class="pv-box">
      <small>INFORMATIONS</small>
      <span>Date : <b>{{ invoice.issue_date.strftime('%d/%m/%Y') }}</b></span>
      {% if invoice.due_date %}<span>{{ "Valable jusqu'au" if invoice.doc_type in ('devis','proforma') else 'Échéance' }} : <b>{{ invoice.due_date.strftime('%d/%m/%Y') }}</b></span>{% endif %}
      {% if invoice.payment_method %}<span>Paiement : <b>{{ invoice.payment_method }}</b></span>{% endif %}
      <span>Devise : <b>{{ currency }}</b></span>
    </div>
  </div>

  <div class="table-wrap">
    <table class="pv-table">
      <thead><tr><th>N°</th><th>Désignation</th><th class="num">Qté</th><th class="num">Prix unitaire</th><th class="num">Montant</th></tr></thead>
      <tbody>{% for line in invoice.lines %}
        <tr><td class="pv-idx">{{ loop.index }}</td><td class="pv-pre">{{ line.designation }}</td><td class="num">{{ line.quantity|num }}</td>
          <td class="num">{{ line.unit_price|money }}</td><td class="num"><b>{{ line.total|money }}</b></td></tr>
      {% endfor %}</tbody>
    </table>
  </div>

  <div class="pv-totals">
    <div><span>Sous-total</span><span>{{ invoice.subtotal|money }}</span></div>
    {% if invoice.discount %}<div><span>Remise</span><span>− {{ invoice.discount|money }}</span></div>{% endif %}
    {% if invoice.tax_rate %}<div><span>TVA ({{ '%g' % invoice.tax_rate }} %)</span><span>{{ invoice.tax_amount|money }}</span></div>{% endif %}
    <div class="pv-grand"><span>TOTAL{{ ' TTC' if invoice.tax_rate }}</span><span>{{ invoice.total|money }}</span></div>
    {% if invoice.amount_paid %}
    <div><span>Acompte / payé</span><span>{{ invoice.amount_paid|money }}</span></div>
    <div class="pv-due"><span>Reste à payer</span><span>{{ invoice.balance|money }}</span></div>
    {% endif %}
  </div>

  <div class="pv-words">
    {{ {'devis': 'Arrêté le présent devis', 'proforma': 'Arrêtée la présente facture proforma'}.get(invoice.doc_type, 'Arrêtée la présente facture') }} à la somme de :<br>
    <b>{{ invoice.total|en_lettres(currency) }}</b> ({{ invoice.total|money }})
  </div>

  {% if invoice.notes %}<div class="pv-note"><small>NOTES</small><span class="pv-pre">{{ invoice.notes }}</span></div>{% endif %}
  {% if settings.invoice_payment_info %}<div class="pv-note"><small>MOYENS DE PAIEMENT</small><span class="pv-pre">{{ settings.invoice_payment_info }}</span></div>{% endif %}
  {% if settings.invoice_terms %}<div class="pv-note"><small>CONDITIONS</small><span class="pv-pre">{{ settings.invoice_terms }}</span></div>{% endif %}

  <div class="pv-signs"><div>Le client<span>Signature</span></div><div>Pour <b>{{ settings.company_name }}</b><span>Signature et cachet</span></div></div>
  {% if settings.invoice_footer %}<div class="pv-footer">{{ settings.invoice_footer }}</div>{% endif %}
</div>

<div class="card" style="margin-top:1.25rem">
  <h2>Lien de partage</h2>
  <p class="muted">Ce lien ouvre le PDF sans connexion. Il est inclus dans le message WhatsApp.</p>
  <div class="swatch-row" style="flex-wrap:wrap">
    <input type="text" readonly value="{{ public_url }}" id="publicUrl" aria-label="Lien public" style="flex:1;min-width:220px">
    <button class="btn btn-light" type="button" id="copyLink">Copier</button>
    <form class="inline" method="post" action="{{ url_for('proinvoices.new_link', invoice_id=invoice.id) }}" data-confirm="Créer un nouveau lien ? L'ancien lien ne fonctionnera plus.">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <button class="btn btn-light" type="submit">Nouveau lien</button>
    </form>
    {% if current_user.effective_role in ('admin','gestionnaire') %}
    <form class="inline" method="post" action="{{ url_for('proinvoices.delete', invoice_id=invoice.id) }}" data-confirm="Supprimer définitivement {{ invoice.number }} ?">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <button class="btn btn-ghost-danger" type="submit">Supprimer</button>
    </form>
    {% endif %}
  </div>
</div>
{% endblock %}

{% block scripts %}
<script>
(function () {
  "use strict";
  var pdfUrl = {{ url_for('proinvoices.pdf', invoice_id=invoice.id)|tojson }};
  var fileName = {{ (invoice.number ~ '.pdf')|tojson }};
  var waUrl = {{ wa_url|tojson }};
  var message = {{ wa_message|tojson }};

  {% if auto_download %}document.getElementById("pdfDownload").click();{% endif %}

  // Sur téléphone : partage du fichier PDF lui-même (on choisit WhatsApp). Sinon : lien WhatsApp.
  // Le PDF est préparé à l'avance pour que le partage parte immédiatement au clic.
  var pdfFile = null;
  if (navigator.canShare && window.File) {
    fetch(pdfUrl, { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error("pdf"); return r.blob(); })
      .then(function (blob) {
        var file = new File([blob], fileName, { type: "application/pdf" });
        if (navigator.canShare({ files: [file] })) pdfFile = file;
      })
      .catch(function () { pdfFile = null; });
  }
  document.getElementById("waShare").addEventListener("click", function () {
    if (!pdfFile) { window.open(waUrl, "_blank", "noopener"); return; }
    navigator.share({ files: [pdfFile], title: fileName, text: message })
      .catch(function (err) { if (!err || err.name !== "AbortError") window.open(waUrl, "_blank", "noopener"); });
  });

  document.getElementById("copyLink").addEventListener("click", function () {
    var input = document.getElementById("publicUrl");
    input.select();
    var done = function () { document.getElementById("copyLink").textContent = "Copié ✓"; };
    if (navigator.clipboard) navigator.clipboard.writeText(input.value).then(done, function () { document.execCommand("copy"); done(); });
    else { document.execCommand("copy"); done(); }
  });
})();
</script>
{% endblock %}
''',

# ---------------------------------------------------------------- proinvoices/form.html
'proinvoices/form.html': r'''{% extends "base.html" %}
{% block title %}{{ 'Modifier ' ~ invoice.number if invoice else 'Nouvelle Facture Pro' }}{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>{{ 'Modifier ' ~ invoice.number if invoice else 'Nouvelle Facture Pro' }}</h1>
    <p>Saisissez le client, puis les produits ou services. Les totaux se calculent automatiquement.</p></div>
  <div class="actions"><a class="btn btn-light" href="{{ url_for('proinvoices.detail', invoice_id=invoice.id) if invoice else url_for('proinvoices.index') }}">Annuler</a></div>
</div>

<form method="post" id="proForm" autocomplete="off">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">

  <div class="grid grid-2">
    <div class="card">
      <h2>👤 Client</h2>
      <div class="form-grid">
        <div class="field full"><label for="client_name">Nom du client / entreprise <span class="req">*</span></label>
          <input type="text" id="client_name" name="client_name" value="{{ v.get('client_name','') }}" maxlength="150" required placeholder="ex. Société Kaloum Services SARL"></div>
        <div class="field full"><label for="client_address">Adresse du client</label>
          <textarea id="client_address" name="client_address" rows="3" placeholder="Quartier, rue, ville…&#10;Une information par ligne">{{ v.get('client_address','') }}</textarea></div>
        <div class="field"><label for="client_phone">Téléphone (WhatsApp)</label>
          <input type="tel" id="client_phone" name="client_phone" value="{{ v.get('client_phone','') }}" placeholder="+224 6xx xx xx xx" inputmode="tel"></div>
        <div class="field"><label for="client_email">E-mail</label>
          <input type="email" id="client_email" name="client_email" value="{{ v.get('client_email','') }}"></div>
        <div class="field full"><label for="client_nif">NIF / RCCM du client</label>
          <input type="text" id="client_nif" name="client_nif" value="{{ v.get('client_nif','') }}" placeholder="Facultatif"></div>
      </div>
    </div>

    <div class="card">
      <h2>📄 Document</h2>
      <div class="form-grid">
        <div class="field full"><label for="doc_type">Type de document</label>
          <select id="doc_type" name="doc_type" {{ 'disabled' if invoice }}>
            {% for key, val in doc_types.items() %}<option value="{{ key }}" {{ 'selected' if v.get('doc_type','facture') == key }}>{{ val[0]|capitalize }}</option>{% endfor %}
          </select>
          {% if invoice %}<input type="hidden" name="doc_type" value="{{ invoice.doc_type }}"><div class="hint">Le type ne change pas après la création (numérotation).</div>{% endif %}</div>
        <div class="field"><label for="issue_date">Date <span class="req">*</span></label>
          <input type="date" id="issue_date" name="issue_date" value="{{ v.get('issue_date','') }}" required></div>
        <div class="field"><label for="due_date">Échéance / validité</label>
          <input type="date" id="due_date" name="due_date" value="{{ v.get('due_date','') }}"></div>
        <div class="field full"><label for="payment_method">Mode de paiement</label>
          <input type="text" id="payment_method" name="payment_method" value="{{ v.get('payment_method','') }}" list="payModes" placeholder="ex. Orange Money, espèces, virement…">
          <datalist id="payModes"><option value="Espèces"><option value="Orange Money"><option value="MTN Mobile Money"><option value="Virement bancaire"><option value="Chèque"><option value="Espèces ou Orange Money"></datalist></div>
      </div>
    </div>
  </div>

  <div class="card flush">
    <div class="card-head"><h2>🧾 Produits et services</h2><span class="muted">{{ settings.currency }}</span></div>
    <div class="table-wrap">
      <table class="line-table pro-lines">
        <thead><tr><th>Désignation</th><th style="width:110px">Quantité</th><th style="width:160px">Prix unitaire</th><th class="num" style="width:150px">Montant</th><th style="width:52px"></th></tr></thead>
        <tbody id="proLines">
          {% for row in rows %}
          <tr class="line">
            <td class="product-cell"><input type="text" name="designation[]" value="{{ row.designation }}" maxlength="500" list="catalog" placeholder="Produit ou service" aria-label="Désignation"></td>
            <td data-label="Quantité"><input class="q" type="number" name="quantity[]" value="{{ row.quantity }}" min="0" step="any" inputmode="decimal" aria-label="Quantité"></td>
            <td data-label="Prix unitaire"><input class="p" type="number" name="unit_price[]" value="{{ row.unit_price }}" min="0" step="any" inputmode="decimal" placeholder="0" aria-label="Prix unitaire"></td>
            <td class="num lt" data-label="Montant">0</td>
            <td><button type="button" class="btn btn-ghost-danger btn-sm rm" aria-label="Supprimer la ligne">✕</button></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    <datalist id="catalog">{% for name in catalog %}<option value="{{ name }}">{% endfor %}</datalist>
    <div style="padding:1rem 1.25rem">
      <button type="button" class="btn btn-light" id="addLine">＋ Ajouter une ligne</button>
    </div>
  </div>

  <div class="grid grid-2">
    <div class="card">
      <h2>📝 Notes</h2>
      <div class="field"><textarea name="notes" rows="5" placeholder="Conditions particulières, délai de livraison, garantie…">{{ v.get('notes','') }}</textarea></div>
      <p class="muted" style="margin:.6rem 0 0">Le logo, l'adresse, la couleur, les moyens de paiement et le pied de page viennent de <a href="{{ url_for('settings.index') }}">Paramètres</a>.</p>
    </div>
    <div class="card">
      <h2>💰 Totaux</h2>
      <div class="form-grid">
        <div class="field"><label for="discount">Remise ({{ settings.currency }})</label>
          <input type="number" id="discount" name="discount" value="{{ v.get('discount','0') }}" min="0" step="any" inputmode="decimal"></div>
        <div class="field"><label for="tax_rate">TVA (%)</label>
          <input type="number" id="tax_rate" name="tax_rate" value="{{ v.get('tax_rate','0') }}" min="0" max="100" step="any" inputmode="decimal"></div>
        <div class="field full"><label for="amount_paid">Acompte déjà payé ({{ settings.currency }})</label>
          <input type="number" id="amount_paid" name="amount_paid" value="{{ v.get('amount_paid','0') }}" min="0" step="any" inputmode="decimal"></div>
      </div>
      <div class="totals" style="margin-top:1rem">
        <div><span>Sous-total</span><span id="tSub">0</span></div>
        <div><span>Remise</span><span id="tDisc">0</span></div>
        <div><span>TVA</span><span id="tTax">0</span></div>
        <div class="grand"><span>TOTAL</span><span id="tTotal">0</span></div>
        <div><span>Reste à payer</span><span id="tDue">0</span></div>
      </div>
    </div>
  </div>

  <div class="card pro-actions">
    <button class="btn btn-light" type="submit" name="action" value="save">💾 Enregistrer</button>
    <button class="btn" type="submit" name="action" value="pdf">📄 Générer le PDF</button>
    <button class="btn btn-whatsapp" type="submit" name="action" value="whatsapp">🟢 Envoyer par WhatsApp</button>
  </div>
</form>
{% endblock %}

{% block scripts %}
<script>
(function () {
  "use strict";
  var catalog = {{ catalog|tojson }};
  var currency = {{ settings.currency|tojson }};
  var body = document.getElementById("proLines");
  function num(v) { var n = parseFloat(String(v || "").replace(/\s/g, "").replace(",", ".")); return isNaN(n) ? 0 : n; }
  function fmt(n) { return ansarFmt(n) + " " + currency; }

  function recalc() {
    var sub = 0;
    body.querySelectorAll("tr.line").forEach(function (tr) {
      var total = num(tr.querySelector(".q").value) * num(tr.querySelector(".p").value);
      sub += total;
      tr.querySelector(".lt").textContent = ansarFmt(total);
    });
    var disc = Math.min(num(document.getElementById("discount").value), sub);
    var tax = (sub - disc) * num(document.getElementById("tax_rate").value) / 100;
    var total = sub - disc + tax;
    var due = Math.max(total - num(document.getElementById("amount_paid").value), 0);
    document.getElementById("tSub").textContent = fmt(sub);
    document.getElementById("tDisc").textContent = "− " + fmt(disc);
    document.getElementById("tTax").textContent = fmt(tax);
    document.getElementById("tTotal").textContent = fmt(total);
    document.getElementById("tDue").textContent = fmt(due);
  }

  function addLine(focus) {
    var tr = body.querySelector("tr.line").cloneNode(true);
    tr.querySelectorAll("input").forEach(function (i) { i.value = i.classList.contains("q") ? "1" : ""; });
    body.appendChild(tr);
    if (focus) tr.querySelector("input").focus();
    recalc();
  }

  document.getElementById("addLine").addEventListener("click", function () { addLine(true); });
  body.addEventListener("click", function (e) {
    if (!e.target.classList.contains("rm")) return;
    var rows = body.querySelectorAll("tr.line");
    var tr = e.target.closest("tr");
    if (rows.length > 1) { tr.remove(); }
    else { tr.querySelectorAll("input").forEach(function (i) { i.value = i.classList.contains("q") ? "1" : ""; }); }
    recalc();
  });
  body.addEventListener("input", function (e) {
    if (e.target.name === "designation[]" && catalog.hasOwnProperty(e.target.value)) {
      var price = e.target.closest("tr").querySelector(".p");
      if (!price.value || num(price.value) === 0) price.value = catalog[e.target.value];
    }
    recalc();
  });
  // Entrée dans le dernier prix : ajoute une ligne
  body.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && e.target.tagName === "INPUT") {
      e.preventDefault();
      var rows = body.querySelectorAll("tr.line");
      if (e.target.closest("tr") === rows[rows.length - 1] && e.target.classList.contains("p")) addLine(true);
    }
  });
  ["discount", "tax_rate", "amount_paid"].forEach(function (id) {
    document.getElementById(id).addEventListener("input", recalc);
  });
  recalc();
})();
</script>
{% endblock %}
''',

# ---------------------------------------------------------------- proinvoices/index.html
'proinvoices/index.html': r'''{% extends "base.html" %}
{% from "_macros.html" import pager, empty with context %}
{% block title %}Facture Pro{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Facture Pro</h1><p>Factures, proformas et devis libres : vous saisissez le client et les lignes vous-même.</p></div>
  <div class="actions">
    <a class="btn btn-light" href="{{ url_for('proinvoices.edit', type='devis') }}">＋ Devis</a>
    <a class="btn btn-light" href="{{ url_for('proinvoices.edit', type='proforma') }}">＋ Proforma</a>
    <a class="btn btn-accent" href="{{ url_for('proinvoices.edit') }}">＋ Nouvelle facture</a>
  </div>
</div>
<form class="filters" method="get">
  <input type="search" name="q" value="{{ q }}" placeholder="Client, numéro, téléphone…" aria-label="Rechercher">
  <select name="type" aria-label="Type">
    <option value="">Tous les documents</option>
    {% for key, val in doc_types.items() %}<option value="{{ key }}" {{ 'selected' if key == doc_type }}>{{ val[0]|capitalize }}</option>{% endfor %}
  </select>
  <button class="btn" type="submit">Filtrer</button>
</form>
<div class="card flush">
  {% if invoices %}
  <div class="table-wrap"><table>
    <thead><tr><th>Numéro</th><th>Client</th><th class="hide-sm">Date</th><th class="num">Total</th><th class="hide-sm">Type</th></tr></thead>
    <tbody>{% for inv in invoices %}
      <tr>
        <td class="nowrap"><a class="cell-title" href="{{ url_for('proinvoices.detail', invoice_id=inv.id) }}">{{ inv.number }}</a></td>
        <td>{{ inv.client_name }}{% if inv.client_phone %}<span class="cell-sub">{{ inv.client_phone }}</span>{% endif %}</td>
        <td class="hide-sm nowrap">{{ inv.issue_date.strftime('%d/%m/%Y') }}</td>
        <td class="num">{{ inv.total|money }}{% if inv.amount_paid and inv.balance > 0 %}<span class="cell-sub" style="color:#b86e00">reste {{ inv.balance|money }}</span>{% endif %}</td>
        <td class="hide-sm"><span class="badge {{ {'facture':'badge-success','proforma':'badge-info','devis':'badge-warning'}[inv.doc_type] }}">{{ inv.title|capitalize }}</span></td>
      </tr>
    {% endfor %}</tbody>
  </table></div>
  {{ pager(pagination, 'proinvoices.index') }}
  {% else %}{{ empty("Aucun document pour le moment. Créez votre première Facture Pro.") }}{% endif %}
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- purchases/detail.html
'purchases/detail.html': r'''{% extends "base.html" %}
{% from "_macros.html" import purchase_badge with context %}
{% block title %}{{ purchase.reference }}{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Achat {{ purchase.reference }} {{ purchase_badge(purchase.status, purchase.status_label) }}</h1>
    <p>{{ purchase.supplier.name }} · créé le {{ purchase.created_at|datetime }}{% if purchase.user %} par {{ purchase.user.full_name }}{% endif %}</p></div>
  <div class="actions"><button class="btn btn-light" type="button" onclick="window.print()">Imprimer</button></div>
</div>

<div class="grid grid-main">
  <div class="card flush">
    <div class="table-wrap"><table>
      <thead><tr><th>Produit</th><th class="num">Qté</th><th class="num">Coût unit.</th><th class="num">Total</th></tr></thead>
      <tbody>{% for i in purchase.items %}
        <tr><td><a href="{{ url_for('products.detail', product_id=i.product_id) }}">{{ i.product.name }}</a><span class="cell-sub">{{ i.product.sku }}</span></td>
          <td class="num">{{ i.quantity|num }}</td><td class="num">{{ i.unit_cost|money }}</td><td class="num">{{ i.line_total|money }}</td></tr>
      {% endfor %}</tbody>
      <tfoot><tr><td colspan="3">Total</td><td class="num">{{ purchase.total|money }}</td></tr></tfoot>
    </table></div>
  </div>
  <div>
    <div class="card">
      <h2>Règlement</h2>
      <dl class="dl"><dt>Total</dt><dd>{{ purchase.total|money }}</dd><dt>Payé</dt><dd>{{ purchase.amount_paid|money }}</dd>
        <dt>Reste</dt><dd style="color:{{ '#c62f2f' if purchase.balance > 0 }}">{{ purchase.balance|money }}</dd>
        {% if purchase.received_at %}<dt>Reçu le</dt><dd>{{ purchase.received_at|datetime }}</dd>{% endif %}</dl>
      {% if purchase.notes %}<p class="muted" style="margin-top:.75rem">{{ purchase.notes }}</p>{% endif %}
    </div>
    {% if current_user.effective_role in ('admin','gestionnaire') %}
      {% if purchase.status == 'commande' %}
      <div class="card">
        <h2>Réception</h2>
        <form method="post" action="{{ url_for('purchases.receive', purchase_id=purchase.id) }}" data-confirm="Confirmer la réception ? Le stock sera augmenté.">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
          <label class="check"><input type="checkbox" name="update_cost" value="1" checked> Mettre à jour les prix d'achat</label>
          <button class="btn btn-block" style="margin-top:.75rem" type="submit">✓ Marchandises reçues</button>
        </form>
        <form method="post" action="{{ url_for('purchases.cancel', purchase_id=purchase.id) }}" data-confirm="Annuler ce bon d'achat ?" style="margin-top:.5rem">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
          <button class="btn btn-ghost-danger btn-block" type="submit">Annuler le bon</button>
        </form>
      </div>
      {% endif %}
      {% if purchase.status != 'annule' and purchase.balance > 0 %}
      <div class="card">
        <h2>Enregistrer un paiement</h2>
        <form method="post" action="{{ url_for('purchases.payment', purchase_id=purchase.id) }}">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
          <div class="field"><input type="number" name="amount" min="1" max="{{ '%g' % purchase.balance }}" step="any" inputmode="decimal" value="{{ '%g' % purchase.balance }}" required aria-label="Montant"></div>
          <button class="btn btn-block" style="margin-top:.6rem" type="submit">Payer le fournisseur</button>
        </form>
      </div>
      {% endif %}
    {% endif %}
  </div>
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- purchases/form.html
'purchases/form.html': r'''{% extends "base.html" %}
{% block title %}Nouvel achat{% endblock %}
{% block content %}
{% set v = form if form else {} %}
{% set selected_supplier = v.get('supplier_id', request.args.get('fournisseur', ''))|string %}
<div class="page-head"><div><h1>Nouvel achat</h1><p>Créez un bon de commande fournisseur. Le stock augmente à la réception.</p></div></div>
<form method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <div class="card">
    <div class="form-grid">
      <div class="field"><label for="supplier_id">Fournisseur <span class="req">*</span></label>
        <select id="supplier_id" name="supplier_id" required>
          <option value="">— Choisir —</option>
          {% for s in suppliers %}<option value="{{ s.id }}" {{ 'selected' if s.id|string == selected_supplier }}>{{ s.name }}</option>{% endfor %}
        </select>
        <div class="hint"><a href="{{ url_for('purchases.supplier_form') }}">＋ Ajouter un fournisseur</a></div></div>
      <div class="field"><label for="notes">Notes</label><input type="text" id="notes" name="notes" value="{{ v.get('notes','') }}"></div>
    </div>
  </div>

  {% with mode='purchase', price_label="Coût unitaire" %}{% include "_line_editor.html" %}{% endwith %}

  <div class="card">
    <div class="form-grid">
      <div class="field"><label for="amount_paid">Montant déjà payé ({{ settings.currency }})</label>
        <input type="number" id="amount_paid" name="amount_paid" min="0" step="any" inputmode="decimal" value="{{ v.get('amount_paid','0') }}"></div>
      <div class="totals"><div class="grand"><span>Total</span><span id="grandTotal">0</span></div></div>
      <div class="field full">
        <label class="check"><input type="checkbox" name="receive_now" value="1" {{ 'checked' if v.get('receive_now') }}> Marchandises reçues immédiatement (ajouter au stock maintenant)</label>
        <label class="check" style="margin-top:.4rem"><input type="checkbox" name="update_cost" value="1" {{ 'checked' if v.get('update_cost', '1') }}> Mettre à jour le prix d'achat des produits avec ces coûts</label>
      </div>
    </div>
    <div class="form-actions">
      <a class="btn btn-light" href="{{ url_for('purchases.index') }}">Annuler</a>
      <button class="btn" type="submit">Enregistrer l'achat</button>
    </div>
  </div>
</form>
{% endblock %}
''',

# ---------------------------------------------------------------- purchases/index.html
'purchases/index.html': r'''{% extends "base.html" %}
{% from "_macros.html" import pager, purchase_badge, empty with context %}
{% block title %}Achats{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Achats</h1><p>Bons de commande et réceptions de marchandises.</p></div>
  <div class="actions">
    <a class="btn btn-light" href="{{ url_for('purchases.export') }}">Exporter</a>
    {% if current_user.effective_role in ('admin','gestionnaire') %}<a class="btn" href="{{ url_for('purchases.create') }}">＋ Nouvel achat</a>{% endif %}
  </div>
</div>
<div class="tabs">
  <a href="{{ url_for('purchases.index') }}" class="{{ 'active' if not status }}">Tous</a>
  {% for k, label in statuses.items() %}<a href="{{ url_for('purchases.index', statut=k) }}" class="{{ 'active' if status == k }}">{{ label }}</a>{% endfor %}
</div>
<div class="card flush">
  {% if purchases %}
  <div class="table-wrap"><table>
    <thead><tr><th>Référence</th><th>Fournisseur</th><th class="hide-sm">Date</th><th class="num">Total</th><th class="num hide-sm">Reste à payer</th><th>Statut</th></tr></thead>
    <tbody>{% for p in purchases %}
      <tr class="{{ 'row-muted' if p.status == 'annule' }}"><td><a href="{{ url_for('purchases.detail', purchase_id=p.id) }}">{{ p.reference }}</a></td>
        <td>{{ p.supplier.name }}</td><td class="hide-sm nowrap">{{ p.created_at|date }}</td>
        <td class="num">{{ p.total|money }}</td><td class="num hide-sm">{{ p.balance|money }}</td><td>{{ purchase_badge(p.status, p.status_label) }}</td></tr>
    {% endfor %}</tbody>
  </table></div>
  {{ pager(pagination, 'purchases.index') }}
  {% else %}{{ empty("Aucun achat enregistré.") }}{% endif %}
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- purchases/supplier_detail.html
'purchases/supplier_detail.html': r'''{% extends "base.html" %}
{% from "_macros.html" import purchase_badge, empty with context %}
{% block title %}{{ supplier.name }}{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>{{ supplier.name }}</h1><p>Fournisseur depuis le {{ supplier.created_at|date }}</p></div>
  {% if current_user.effective_role in ('admin','gestionnaire') %}
  <div class="actions">
    <a class="btn btn-light" href="{{ url_for('purchases.supplier_form', supplier_id=supplier.id) }}">Modifier</a>
    <a class="btn" href="{{ url_for('purchases.create', fournisseur=supplier.id) }}">＋ Nouvel achat</a>
  </div>{% endif %}
</div>
<div class="grid grid-main">
  <div class="card flush">
    <div class="card-head"><h2>Historique des achats</h2></div>
    {% if purchases %}
    <div class="table-wrap"><table>
      <thead><tr><th>Référence</th><th>Date</th><th class="num">Total</th><th class="num hide-sm">Reste</th><th>Statut</th></tr></thead>
      <tbody>{% for p in purchases %}
        <tr><td><a href="{{ url_for('purchases.detail', purchase_id=p.id) }}">{{ p.reference }}</a></td><td class="nowrap">{{ p.created_at|date }}</td>
          <td class="num">{{ p.total|money }}</td><td class="num hide-sm">{{ p.balance|money }}</td><td>{{ purchase_badge(p.status, p.status_label) }}</td></tr>
      {% endfor %}</tbody>
    </table></div>
    {% else %}{{ empty("Aucun achat auprès de ce fournisseur.") }}{% endif %}
  </div>
  <div class="card">
    <h2>Coordonnées</h2>
    <dl class="dl">
      <dt>Contact</dt><dd>{{ supplier.contact or '—' }}</dd>
      <dt>Téléphone</dt><dd>{% if supplier.phone %}<a href="tel:{{ supplier.phone|replace(' ','') }}">{{ supplier.phone }}</a>{% else %}—{% endif %}</dd>
      <dt>E-mail</dt><dd>{{ supplier.email or '—' }}</dd>
      <dt>Adresse</dt><dd>{{ supplier.address or '—' }}</dd>
      <dt>Total acheté</dt><dd>{{ total|money }}</dd>
      <dt>Dette</dt><dd style="color:{{ '#c62f2f' if debt > 0 }}">{{ debt|money }}</dd>
    </dl>
    {% if supplier.notes %}<p style="margin-top:1rem;white-space:pre-wrap">{{ supplier.notes }}</p>{% endif %}
  </div>
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- purchases/supplier_form.html
'purchases/supplier_form.html': r'''{% extends "base.html" %}
{% block title %}Fournisseur{% endblock %}
{% block content %}
{% set v = form if form else {} %}
<div class="page-head"><div><h1>{{ 'Modifier ' ~ supplier.name if supplier else 'Nouveau fournisseur' }}</h1></div></div>
<div class="card" style="max-width:760px">
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="form-grid">
      <div class="field full"><label for="name">Nom / raison sociale <span class="req">*</span></label>
        <input type="text" id="name" name="name" value="{{ v.get('name', supplier.name if supplier else '') }}" required maxlength="150"></div>
      <div class="field"><label for="contact">Personne à contacter</label><input type="text" id="contact" name="contact" value="{{ v.get('contact', (supplier.contact or '') if supplier else '') }}"></div>
      <div class="field"><label for="phone">Téléphone</label><input type="tel" id="phone" name="phone" value="{{ v.get('phone', (supplier.phone or '') if supplier else '') }}" placeholder="+224 6xx xx xx xx"></div>
      <div class="field"><label for="email">E-mail</label><input type="email" id="email" name="email" value="{{ v.get('email', (supplier.email or '') if supplier else '') }}"></div>
      <div class="field"><label for="address">Adresse</label><input type="text" id="address" name="address" value="{{ v.get('address', (supplier.address or '') if supplier else '') }}"></div>
      <div class="field full"><label for="notes">Notes</label><textarea id="notes" name="notes">{{ v.get('notes', (supplier.notes or '') if supplier else '') }}</textarea></div>
    </div>
    <div class="form-actions">
      <a class="btn btn-light" href="{{ url_for('purchases.supplier_detail', supplier_id=supplier.id) if supplier else url_for('purchases.suppliers') }}">Annuler</a>
      <button class="btn" type="submit">Enregistrer</button>
    </div>
  </form>
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- purchases/suppliers.html
'purchases/suppliers.html': r'''{% extends "base.html" %}
{% from "_macros.html" import empty with context %}
{% block title %}Fournisseurs{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Fournisseurs</h1><p>{{ suppliers|length }} fournisseur(s)</p></div>
  {% if current_user.effective_role in ('admin','gestionnaire') %}<div class="actions"><a class="btn" href="{{ url_for('purchases.supplier_form') }}">＋ Nouveau fournisseur</a></div>{% endif %}
</div>
<form class="filters" method="get">
  <input type="search" name="q" value="{{ q }}" placeholder="Nom, contact ou téléphone…" aria-label="Rechercher">
  <button class="btn" type="submit">Rechercher</button>
</form>
<div class="card flush">
  {% if suppliers %}
  <div class="table-wrap"><table>
    <thead><tr><th>Fournisseur</th><th>Téléphone</th><th class="hide-sm">Adresse</th><th class="num hide-sm">Achats</th></tr></thead>
    <tbody>{% for s in suppliers %}
      <tr><td><a class="cell-title" href="{{ url_for('purchases.supplier_detail', supplier_id=s.id) }}">{{ s.name }}</a>{% if s.contact %}<span class="cell-sub">{{ s.contact }}</span>{% endif %}</td>
        <td class="nowrap">{% if s.phone %}<a href="tel:{{ s.phone|replace(' ','') }}">{{ s.phone }}</a>{% else %}—{% endif %}</td>
        <td class="hide-sm">{{ s.address or '—' }}</td><td class="num hide-sm">{{ s.purchases|length }}</td></tr>
    {% endfor %}</tbody>
  </table></div>
  {% else %}{{ empty("Aucun fournisseur.") }}{% endif %}
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- sales/customer_detail.html
'sales/customer_detail.html': r'''{% extends "base.html" %}
{% from "_macros.html" import sale_badge, empty with context %}
{% block title %}{{ customer.name }}{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>{{ customer.name }}</h1><p>Client depuis le {{ customer.created_at|date }}</p></div>
  <div class="actions">
    <a class="btn btn-light" href="{{ url_for('sales.customer_form', customer_id=customer.id) }}">Modifier</a>
    <a class="btn btn-accent" href="{{ url_for('sales.create', client=customer.id) }}">＋ Nouvelle vente</a>
  </div>
</div>
<div class="grid grid-main">
  <div class="card flush">
    <div class="card-head"><h2>Historique des achats</h2></div>
    {% if sales %}
    <div class="table-wrap"><table>
      <thead><tr><th>Facture</th><th>Date</th><th class="num">Total</th><th class="num hide-sm">Reste</th><th>Statut</th></tr></thead>
      <tbody>{% for s in sales %}
        <tr><td><a href="{{ url_for('sales.detail', sale_id=s.id) }}">{{ s.invoice_number }}</a></td><td class="nowrap">{{ s.created_at|date }}</td>
          <td class="num">{{ s.total|money }}</td><td class="num hide-sm">{{ s.balance|money }}</td><td>{{ sale_badge(s.status, s.status_label) }}</td></tr>
      {% endfor %}</tbody>
    </table></div>
    {% else %}{{ empty("Aucun achat pour ce client.") }}{% endif %}
  </div>
  <div class="card">
    <h2>Coordonnées</h2>
    <dl class="dl">
      <dt>Téléphone</dt><dd>{% if customer.phone %}<a href="tel:{{ customer.phone|replace(' ','') }}">{{ customer.phone }}</a>{% else %}—{% endif %}</dd>
      <dt>E-mail</dt><dd>{{ customer.email or '—' }}</dd>
      <dt>Adresse</dt><dd>{{ customer.address or '—' }}</dd>
      <dt>Total acheté</dt><dd>{{ total|money }}</dd>
      <dt>Doit encore</dt><dd style="color:{{ '#c62f2f' if debt > 0 }}">{{ debt|money }}</dd>
    </dl>
  </div>
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- sales/customer_form.html
'sales/customer_form.html': r'''{% extends "base.html" %}
{% block title %}Client{% endblock %}
{% block content %}
{% set v = form if form else {} %}
<div class="page-head"><div><h1>{{ 'Modifier ' ~ customer.name if customer else 'Nouveau client' }}</h1></div></div>
<div class="card" style="max-width:760px">
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="form-grid">
      <div class="field full"><label for="name">Nom <span class="req">*</span></label><input type="text" id="name" name="name" value="{{ v.get('name', customer.name if customer else '') }}" required maxlength="150"></div>
      <div class="field"><label for="phone">Téléphone</label><input type="tel" id="phone" name="phone" value="{{ v.get('phone', (customer.phone or '') if customer else '') }}" placeholder="+224 6xx xx xx xx"></div>
      <div class="field"><label for="email">E-mail</label><input type="email" id="email" name="email" value="{{ v.get('email', (customer.email or '') if customer else '') }}"></div>
      <div class="field full"><label for="address">Adresse</label><input type="text" id="address" name="address" value="{{ v.get('address', (customer.address or '') if customer else '') }}"></div>
    </div>
    <div class="form-actions">
      <a class="btn btn-light" href="{{ url_for('sales.customer_detail', customer_id=customer.id) if customer else url_for('sales.customers') }}">Annuler</a>
      <button class="btn" type="submit">Enregistrer</button>
    </div>
  </form>
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- sales/customers.html
'sales/customers.html': r'''{% extends "base.html" %}
{% from "_macros.html" import empty with context %}
{% block title %}Clients{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Clients</h1><p>{{ customers|length }} client(s)</p></div>
  <div class="actions"><a class="btn" href="{{ url_for('sales.customer_form') }}">＋ Nouveau client</a></div>
</div>
<form class="filters" method="get">
  <input type="search" name="q" value="{{ q }}" placeholder="Nom ou téléphone…" aria-label="Rechercher">
  <button class="btn" type="submit">Rechercher</button>
</form>
<div class="card flush">
  {% if customers %}
  <div class="table-wrap"><table>
    <thead><tr><th>Client</th><th>Téléphone</th><th class="hide-sm">Adresse</th><th class="num hide-sm">Achats</th></tr></thead>
    <tbody>{% for c in customers %}
      <tr><td><a class="cell-title" href="{{ url_for('sales.customer_detail', customer_id=c.id) }}">{{ c.name }}</a></td>
        <td class="nowrap">{% if c.phone %}<a href="tel:{{ c.phone|replace(' ','') }}">{{ c.phone }}</a>{% else %}—{% endif %}</td>
        <td class="hide-sm">{{ c.address or '—' }}</td><td class="num hide-sm">{{ c.sales|length }}</td></tr>
    {% endfor %}</tbody>
  </table></div>
  {% else %}{{ empty("Aucun client.") }}{% endif %}
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- sales/detail.html
'sales/detail.html': r'''{% extends "base.html" %}
{% from "_macros.html" import sale_badge with context %}
{% block title %}{{ sale.invoice_number }}{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Facture {{ sale.invoice_number }} {{ sale_badge(sale.status, sale.status_label) }}</h1>
    <p>{{ sale.created_at|datetime }} · {{ sale.customer.name if sale.customer else 'Client comptant' }}{% if sale.user %} · vendu par {{ sale.user.full_name }}{% endif %}</p></div>
  <div class="actions">
    <a class="btn btn-light" href="{{ url_for('sales.invoice', sale_id=sale.id, format='ticket') }}" target="_blank" rel="noopener">Ticket de caisse</a>
    <a class="btn" href="{{ url_for('sales.invoice', sale_id=sale.id) }}" target="_blank" rel="noopener">🖨 Facture A4</a>
  </div>
</div>
<div class="grid grid-main">
  <div class="card flush">
    <div class="table-wrap"><table>
      <thead><tr><th>Produit</th><th class="num">Qté</th><th class="num">Prix unit.</th><th class="num">Total</th></tr></thead>
      <tbody>{% for i in sale.items %}
        <tr><td><a href="{{ url_for('products.detail', product_id=i.product_id) }}">{{ i.product.name }}</a><span class="cell-sub">{{ i.product.sku }}</span></td>
          <td class="num">{{ i.quantity|num }}</td><td class="num">{{ i.unit_price|money }}</td><td class="num">{{ i.line_total|money }}</td></tr>
      {% endfor %}</tbody>
    </table></div>
    <div style="padding:1rem 1.25rem">
      <div class="totals">
        <div><span>Sous-total</span><span>{{ sale.subtotal|money }}</span></div>
        {% if sale.discount %}<div><span>Remise</span><span>− {{ sale.discount|money }}</span></div>{% endif %}
        {% if sale.tax_amount %}<div><span>Taxe ({{ '%g' % sale.tax_rate }} %)</span><span>{{ sale.tax_amount|money }}</span></div>{% endif %}
        <div class="grand"><span>Total</span><span>{{ sale.total|money }}</span></div>
      </div>
    </div>
  </div>
  <div>
    <div class="card">
      <h2>Paiement</h2>
      <dl class="dl"><dt>Mode</dt><dd>{{ sale.payment_label }}</dd><dt>Payé</dt><dd>{{ sale.amount_paid|money }}</dd>
        <dt>Reste</dt><dd style="color:{{ '#c62f2f' if sale.balance > 0 and sale.status != 'annulee' }}">{{ sale.balance|money }}</dd>
        {% if current_user.effective_role in ('admin','gestionnaire') %}<dt>Bénéfice</dt><dd>{{ sale.profit|money }}</dd>{% endif %}</dl>
      {% if sale.customer %}<p style="margin-top:.75rem"><a href="{{ url_for('sales.customer_detail', customer_id=sale.customer.id) }}">Fiche client →</a></p>{% endif %}
      {% if sale.notes %}<p class="muted" style="white-space:pre-wrap">{{ sale.notes }}</p>{% endif %}
    </div>
    {% if sale.status in ('partielle','impayee') %}
    <div class="card">
      <h2>Encaisser un paiement</h2>
      <form method="post" action="{{ url_for('sales.payment', sale_id=sale.id) }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="number" name="amount" min="1" max="{{ '%g' % sale.balance }}" step="any" inputmode="decimal" value="{{ '%g' % sale.balance }}" required aria-label="Montant">
        <button class="btn btn-block" style="margin-top:.6rem" type="submit">Enregistrer le paiement</button>
      </form>
    </div>
    {% endif %}
    {% if sale.status != 'annulee' and current_user.effective_role in ('admin','gestionnaire') %}
    <div class="card">
      <h2>Annuler la vente</h2>
      <form method="post" action="{{ url_for('sales.cancel', sale_id=sale.id) }}" data-confirm="Annuler cette vente ? Les produits seront remis en stock.">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="text" name="reason" placeholder="Motif de l'annulation" required aria-label="Motif">
        <button class="btn btn-ghost-danger btn-block" style="margin-top:.6rem" type="submit">Annuler la vente</button>
      </form>
    </div>
    {% endif %}
  </div>
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- sales/form.html
'sales/form.html': r'''{% extends "base.html" %}
{% block title %}Nouvelle vente{% endblock %}
{% block content %}
{% set v = form if form else {} %}
<div class="page-head"><div><h1>Nouvelle vente</h1><p>Ajoutez les produits, choisissez le client et le mode de paiement.</p></div></div>
<form method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">

  {% with mode='sale', price_label="Prix unitaire" %}{% include "_line_editor.html" %}{% endwith %}

  <div class="grid grid-2">
    <div class="card">
      <h2>Client</h2>
      <div class="field"><label for="customer_id">Client existant</label>
        <select id="customer_id" name="customer_id">
          <option value="">Client comptant (non identifié)</option>
          {% for c in customers %}<option value="{{ c.id }}" {{ 'selected' if c.id|string == v.get('customer_id', request.args.get('client',''))|string }}>{{ c.name }}{% if c.phone %} · {{ c.phone }}{% endif %}</option>{% endfor %}
        </select></div>
      <p class="muted" style="margin:.75rem 0 .5rem">… ou nouveau client :</p>
      <div class="form-grid">
        <div class="field"><input type="text" name="new_customer_name" value="{{ v.get('new_customer_name','') }}" placeholder="Nom du client" aria-label="Nom du nouveau client"></div>
        <div class="field"><input type="tel" name="new_customer_phone" value="{{ v.get('new_customer_phone','') }}" placeholder="Téléphone" aria-label="Téléphone du nouveau client"></div>
      </div>
      <div class="field" style="margin-top:1rem"><label for="notes">Note sur la facture</label><input type="text" id="notes" name="notes" value="{{ v.get('notes','') }}"></div>
    </div>

    <div class="card">
      <h2>Paiement</h2>
      <div class="form-grid">
        <div class="field"><label for="payment_method">Mode de paiement</label>
          <select id="payment_method" name="payment_method">
            {% for k, label in payment_methods.items() %}<option value="{{ k }}" {{ 'selected' if v.get('payment_method','especes') == k }}>{{ label }}</option>{% endfor %}
          </select></div>
        <div class="field"><label for="discount">Remise ({{ settings.currency }})</label>
          <input type="number" id="discount" name="discount" min="0" step="any" inputmode="decimal" value="{{ v.get('discount','0') }}"></div>
        <div class="field full"><label for="amount_paid">Montant reçu ({{ settings.currency }})</label>
          <input type="number" id="amount_paid" name="amount_paid" min="0" step="any" inputmode="decimal" value="{{ v.get('amount_paid','') }}" placeholder="Vide = paiement complet">
          <div class="hint" id="changeInfo"></div></div>
      </div>
      <div class="totals" style="margin-top:1rem">
        <div><span>Sous-total</span><span id="subtotal">0</span></div>
        {% if tax_rate %}<div><span>Taxe ({{ '%g' % tax_rate }} %)</span><span id="taxAmount">0</span></div>{% endif %}
        <div class="grand"><span>Total à payer</span><span id="grandTotal">0</span></div>
      </div>
      <div class="form-actions">
        <a class="btn btn-light" href="{{ url_for('sales.index') }}">Annuler</a>
        <button class="btn btn-accent" type="submit">✓ Valider la vente</button>
      </div>
    </div>
  </div>
</form>
{% endblock %}
''',

# ---------------------------------------------------------------- sales/index.html
'sales/index.html': r'''{% extends "base.html" %}
{% from "_macros.html" import pager, sale_badge, empty with context %}
{% block title %}Ventes{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Ventes &amp; factures</h1><p>{{ pagination.total }} vente(s) · total (hors annulées) : <strong>{{ total|money }}</strong></p></div>
  <div class="actions">
    <a class="btn btn-light" href="{{ url_for('sales.export', **args) }}">Exporter</a>
    <a class="btn btn-accent" href="{{ url_for('sales.create') }}">＋ Nouvelle vente</a>
  </div>
</div>
<form class="filters" method="get">
  <input type="search" name="q" value="{{ args.get('q','') }}" placeholder="N° facture, client, téléphone…" aria-label="Rechercher">
  <select name="statut" aria-label="Statut"><option value="">Tous les statuts</option>
    {% for k, label in statuses.items() %}<option value="{{ k }}" {{ 'selected' if args.get('statut') == k }}>{{ label }}</option>{% endfor %}</select>
  <input type="date" name="du" value="{{ args.get('du','') }}" aria-label="Du">
  <input type="date" name="au" value="{{ args.get('au','') }}" aria-label="Au">
  <button class="btn" type="submit">Filtrer</button>
</form>
<div class="card flush">
  {% if sales %}
  <div class="table-wrap"><table>
    <thead><tr><th>Facture</th><th class="hide-sm">Date</th><th>Client</th><th class="num">Total</th><th class="num hide-sm">Reste</th><th>Statut</th></tr></thead>
    <tbody>{% for s in sales %}
      <tr class="{{ 'row-muted' if s.status == 'annulee' }}">
        <td><a href="{{ url_for('sales.detail', sale_id=s.id) }}">{{ s.invoice_number }}</a><span class="cell-sub">{{ s.payment_label }}</span></td>
        <td class="hide-sm nowrap">{{ s.created_at|datetime }}</td>
        <td>{{ s.customer.name if s.customer else 'Client comptant' }}</td>
        <td class="num">{{ s.total|money }}</td><td class="num hide-sm">{{ s.balance|money if s.status != 'annulee' else '—' }}</td>
        <td>{{ sale_badge(s.status, s.status_label) }}</td></tr>
    {% endfor %}</tbody>
  </table></div>
  {{ pager(pagination, 'sales.index') }}
  {% else %}{{ empty("Aucune vente trouvée.") }}{% endif %}
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- sales/invoice.html
'sales/invoice.html': r'''<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Facture {{ sale.invoice_number }}</title>
{% set s = settings %}
{% set color = s.invoice_color if s.invoice_color and s.invoice_color|length == 7 else '#0b5d43' %}
<style>
  :root { --brand: {{ color }}; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #e9ecea; color: #111; font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
  .toolbar { display: flex; gap: .5rem; justify-content: center; padding: .75rem; flex-wrap: wrap; }
  .toolbar button, .toolbar a { padding: .6rem 1.1rem; border-radius: 8px; border: 1px solid #cfd6d2; background: #fff; font: inherit; font-weight: 600; cursor: pointer; color: #111; text-decoration: none; }
  .toolbar .primary { background: var(--brand); color: #fff; border-color: var(--brand); }
  .sheet { background: #fff; margin: 0 auto 2rem; padding: 2.2rem 2.4rem; max-width: 210mm; min-height: 270mm; box-shadow: 0 4px 24px rgba(0,0,0,.12); position: relative; }
  .head { display: flex; justify-content: space-between; gap: 1.5rem; flex-wrap: wrap; border-bottom: 3px solid var(--brand); padding-bottom: 1rem; }
  .company { display: flex; gap: .8rem; }
  .company img { width: 72px; height: 72px; object-fit: contain; }
  .initial { width: 56px; height: 56px; border-radius: 10px; background: var(--brand); color: #fff; display: grid; place-items: center; font-size: 1.6rem; font-weight: 800; flex-shrink: 0; }
  .addr { white-space: pre-line; }
  .extra { margin-top: 1.2rem; display: grid; gap: .6rem; font-size: .85rem; }
  .extra div { border-left: 3px solid var(--brand); padding: .3rem .7rem; background: #f7f9f8; }
  .extra small { display: block; text-transform: uppercase; letter-spacing: .05em; color: #666; font-size: .68rem; }
  .company h1 { margin: 0; font-size: 1.35rem; color: var(--brand); }
  .company p, .meta p { margin: .1rem 0; font-size: .85rem; color: #444; }
  .meta { text-align: right; }
  .meta h2 { margin: 0 0 .3rem; font-size: 1.5rem; letter-spacing: .05em; }
  .parties { display: flex; justify-content: space-between; gap: 1rem; margin: 1.4rem 0; flex-wrap: wrap; }
  .box { background: #f4f7f5; border-radius: 8px; padding: .75rem 1rem; min-width: 220px; }
  .box small { text-transform: uppercase; letter-spacing: .06em; color: #666; font-size: .7rem; }
  table { width: 100%; border-collapse: collapse; margin-top: .5rem; }
  th { background: var(--brand); color: #fff; text-align: left; padding: .55rem .6rem; font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; }
  td { padding: .55rem .6rem; border-bottom: 1px solid #e3e7e5; }
  .r { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .totals { margin: 1rem 0 0 auto; width: 300px; max-width: 100%; }
  .totals div { display: flex; justify-content: space-between; padding: .3rem 0; }
  .totals .grand { border-top: 2px solid #111; font-size: 1.15rem; font-weight: 800; margin-top: .3rem; padding-top: .5rem; }
  .status { display: inline-block; padding: .2rem .7rem; border-radius: 99px; font-weight: 700; font-size: .8rem; background: #e3f1ea; color: #08442f; }
  .status.annulee { background: #fbe7e7; color: #8f1f1f; }
  .footer { position: absolute; left: 2.4rem; right: 2.4rem; bottom: 1.8rem; text-align: center; font-size: .8rem; color: #555; border-top: 1px solid #ddd; padding-top: .7rem; }
  .stamp { margin-top: 2.5rem; display: flex; justify-content: flex-end; }
  .stamp div { width: 220px; text-align: center; border-top: 1px dashed #999; padding-top: .4rem; font-size: .8rem; color: #555; }
  .watermark { position: absolute; top: 40%; left: 0; right: 0; text-align: center; font-size: 5rem; color: rgba(198,47,47,.13); transform: rotate(-20deg); font-weight: 900; pointer-events: none; }

  /* Ticket de caisse 80 mm */
  .ticket .sheet { max-width: 80mm; min-height: 0; padding: 4mm; font-size: 12px; }
  .ticket .head { flex-direction: column; align-items: center; text-align: center; border-bottom: 1px dashed #000; }
  .ticket .company { flex-direction: column; align-items: center; }
  .ticket .company img { width: 36px; height: 36px; }
  .ticket .meta { text-align: center; }
  .ticket .meta h2 { font-size: 1rem; }
  .ticket .parties { margin: .5rem 0; }
  .ticket .box { background: none; padding: 0; min-width: 0; }
  .ticket th { background: none; color: #000; border-bottom: 1px dashed #000; padding: .2rem; font-size: 10px; }
  .ticket td { padding: .2rem; border-bottom: 0; }
  .ticket .totals { width: 100%; }
  .ticket .footer { position: static; margin-top: .75rem; }
  .ticket .stamp { display: none; }

  @media (max-width: 640px) {
    .sheet { padding: 1.2rem; min-height: 0; }
    .footer { position: static; margin-top: 2rem; }
    .meta { text-align: left; }
    .hide-sm { display: none; }
  }
  @media print {
    body { background: #fff; }
    .toolbar { display: none; }
    .sheet { box-shadow: none; margin: 0; max-width: none; min-height: 0; }
    .ticket .sheet { width: 80mm; }
    @page { margin: 12mm; }
    th { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  }
</style>
</head>
<body class="{{ 'ticket' if ticket }}">
<div class="toolbar">
  <button class="primary" onclick="window.print()">🖨 Imprimer / Enregistrer en PDF</button>
  {% if ticket %}<a href="{{ url_for('sales.invoice', sale_id=sale.id) }}">Format A4</a>{% else %}<a href="{{ url_for('sales.invoice', sale_id=sale.id, format='ticket') }}">Format ticket</a>{% endif %}
  <a href="{{ url_for('sales.detail', sale_id=sale.id) }}">← Retour</a>
</div>
<div class="sheet">
  {% if sale.status == 'annulee' %}<div class="watermark">ANNULÉE</div>{% endif %}
  <div class="head">
    <div class="company">
      {% if s.show_logo_on_invoice == '1' %}
        {% if company and company.logo %}<img src="{{ url_for('settings.logo', v=company.id) }}" alt="">
        {% else %}<div class="initial">{{ s.company_name[:1]|upper }}</div>{% endif %}
      {% endif %}
      <div>
        <h1>{{ s.company_name }}</h1>
        {% if s.company_activity %}<p><em>{{ s.company_activity }}</em></p>{% endif %}
        {% if s.invoice_address_display %}<p class="addr">{{ s.invoice_address_display }}</p>{% endif %}
        {% if s.invoice_phone_display or s.invoice_email_display %}<p>{% if s.invoice_phone_display %}Tél : {{ s.invoice_phone_display }}{% endif %}{% if s.invoice_phone_display and s.invoice_email_display %} · {% endif %}{{ s.invoice_email_display }}</p>{% endif %}
        {% if s.company_website %}<p>{{ s.company_website }}</p>{% endif %}
        {% if s.company_rccm or s.company_nif %}<p>{% if s.company_rccm %}RCCM : {{ s.company_rccm }}{% endif %}{% if s.company_rccm and s.company_nif %} · {% endif %}{% if s.company_nif %}NIF : {{ s.company_nif }}{% endif %}</p>{% endif %}
      </div>
    </div>
    <div class="meta">
      <h2>{{ (s.invoice_title or 'FACTURE')|upper }}</h2>
      <p><strong>N° {{ sale.invoice_number }}</strong></p>
      <p>Date : {{ sale.created_at|datetime }}</p>
      <p><span class="status {{ sale.status }}">{{ sale.status_label }}</span></p>
    </div>
  </div>

  <div class="parties">
    <div class="box"><small>Client</small><br>
      {% if sale.customer %}<strong>{{ sale.customer.name }}</strong>{% if sale.customer.phone %}<br>{{ sale.customer.phone }}{% endif %}{% if sale.customer.address %}<br>{{ sale.customer.address }}{% endif %}
      {% else %}<strong>Client comptant</strong>{% endif %}</div>
    <div class="box"><small>Paiement</small><br><strong>{{ sale.payment_label }}</strong>{% if sale.user %}<br>Vendeur : {{ sale.user.full_name }}{% endif %}</div>
  </div>

  <table>
    <thead><tr><th>Désignation</th><th class="r">Qté</th><th class="r hide-sm">P.U.</th><th class="r">Montant</th></tr></thead>
    <tbody>{% for i in sale.items %}
      <tr><td>{{ i.product.name }}</td><td class="r">{{ i.quantity|num }}</td><td class="r hide-sm">{{ i.unit_price|money(s.currency) }}</td><td class="r">{{ i.line_total|money(s.currency) }}</td></tr>
    {% endfor %}</tbody>
  </table>

  <div class="totals">
    <div><span>Sous-total</span><span>{{ sale.subtotal|money(s.currency) }}</span></div>
    {% if sale.discount %}<div><span>Remise</span><span>− {{ sale.discount|money(s.currency) }}</span></div>{% endif %}
    {% if sale.tax_amount %}<div><span>Taxe ({{ '%g' % sale.tax_rate }} %)</span><span>{{ sale.tax_amount|money(s.currency) }}</span></div>{% endif %}
    <div class="grand"><span>TOTAL</span><span>{{ sale.total|money(s.currency) }}</span></div>
    <div><span>Montant payé</span><span>{{ sale.amount_paid|money(s.currency) }}</span></div>
    {% if sale.balance > 0 and sale.status != 'annulee' %}<div><strong>Reste à payer</strong><strong>{{ sale.balance|money(s.currency) }}</strong></div>{% endif %}
  </div>

  {% if s.invoice_payment_info or s.invoice_terms %}
  <div class="extra">
    {% if s.invoice_payment_info %}<div><small>Moyens de paiement</small><span class="addr">{{ s.invoice_payment_info }}</span></div>{% endif %}
    {% if s.invoice_terms %}<div><small>Conditions</small><span class="addr">{{ s.invoice_terms }}</span></div>{% endif %}
  </div>
  {% endif %}
  <div class="stamp"><div>Signature et cachet</div></div>
  <div class="footer">{% if s.invoice_footer %}{{ s.invoice_footer }}<br>{% endif %}{{ s.company_name }}{% if s.company_address %} — {{ s.company_address }}{% endif %}<br><small>Logiciel ANSARTECH 224</small></div>
</div>
</body>
</html>
''',

# ---------------------------------------------------------------- settings/index.html
'settings/index.html': r'''{% extends "base.html" %}
{% block title %}Paramètres{% endblock %}
{% block content %}
{% set v = values %}
<div class="page-head">
  <div><h1>Paramètres de l'entreprise</h1><p>Identité, adresses, logo et présentation de vos factures.</p></div>
  <div class="actions">
    <a class="btn btn-light" href="{{ url_for('settings.logs') }}">Journal d'activité</a>
    <a class="btn btn-light" href="{{ url_for('settings.backup') }}">⬇ Sauvegarde complète</a>
  </div>
</div>

<div class="card">
  <dl class="dl">
    <dt>Abonnement</dt><dd>{% if company.subscription_end %}jusqu'au <strong>{{ company.subscription_end.strftime('%d/%m/%Y') }}</strong>{% else %}sans limite{% endif %}</dd>
    <dt>Utilisateurs autorisés</dt><dd>{{ company.max_users }}</dd>
    <dt>Code entreprise</dt><dd>{{ company.code }}</dd>
  </dl>
</div>

<form method="post" enctype="multipart/form-data">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">

  <div class="card">
    <h2>Identité de l'entreprise</h2>
    <div class="form-grid">
      <div class="field"><label for="name">Nom de l'entreprise <span class="req">*</span></label><input type="text" id="name" name="name" value="{{ v.get('name', v.company_name) }}" required maxlength="150"></div>
      <div class="field"><label for="activity">Activité</label><input type="text" id="activity" name="activity" value="{{ v.get('activity', v.company_activity) }}"></div>
      <div class="field"><label for="rccm">RCCM</label><input type="text" id="rccm" name="rccm" value="{{ v.get('rccm', v.company_rccm) }}"></div>
      <div class="field"><label for="nif">NIF</label><input type="text" id="nif" name="nif" value="{{ v.get('nif', v.company_nif) }}"></div>
    </div>
  </div>

  <div class="card">
    <h2>Adresse et coordonnées</h2>
    <div class="form-grid">
      <div class="field full"><label for="address">Adresse</label><input type="text" id="address" name="address" value="{{ v.get('address', v.company_street) }}" placeholder="Quartier, rue, immeuble…"></div>
      <div class="field"><label for="city">Ville</label><input type="text" id="city" name="city" value="{{ v.get('city', v.company_city) }}"></div>
      <div class="field"><label for="country">Pays</label><input type="text" id="country" name="country" value="{{ v.get('country', v.company_country) }}"></div>
      <div class="field"><label for="phone">Téléphone</label><input type="tel" id="phone" name="phone" value="{{ v.get('phone', v.company_phone) }}"></div>
      <div class="field"><label for="email">E-mail</label><input type="email" id="email" name="email" value="{{ v.get('email', v.company_email) }}"></div>
      <div class="field"><label for="website">Site web</label><input type="text" id="website" name="website" value="{{ v.get('website', v.company_website) }}"></div>
    </div>
  </div>

  <div class="card">
    <h2>Logo</h2>
    <div class="swatch-row" style="flex-wrap:wrap;gap:1rem">
      {% if company.logo %}<img class="logo-preview" src="{{ url_for('settings.logo', v=company.id) }}" alt="Logo actuel">{% else %}<div class="logo-preview" style="display:grid;place-items:center;color:var(--muted);font-size:.8rem">Aucun logo</div>{% endif %}
      <div class="field" style="flex:1;min-width:220px"><label for="logo">Nouveau logo</label><input type="file" id="logo" name="logo" accept=".png,.jpg,.jpeg,.webp">
        <div class="hint">PNG, JPG ou WEBP, 1 Mo maximum. Idéalement carré, fond transparent ou blanc.</div>
        {% if company.logo %}<label class="check" style="margin-top:.5rem"><input type="checkbox" name="remove_logo" value="1"> Supprimer le logo actuel</label>{% endif %}</div>
    </div>
  </div>

  <div class="card">
    <h2>Facture</h2>
    <p class="muted">Ces informations apparaissent sur vos factures et tickets. Laissez l'adresse, le téléphone ou l'e-mail de facturation vides pour utiliser ceux de l'entreprise.</p>
    <div class="form-grid">
      <div class="field"><label for="invoice_title">Titre du document</label><input type="text" id="invoice_title" name="invoice_title" value="{{ v.invoice_title }}" maxlength="40" placeholder="FACTURE"></div>
      <div class="field"><label for="invoice_color">Couleur de la facture</label>
        <div class="swatch-row"><input type="color" id="invoice_color" name="invoice_color" value="{{ v.invoice_color or '#0b5d43' }}"><span class="muted">En-têtes et bandeau du tableau</span></div></div>
      <div class="field full"><label for="invoice_address">Adresse de facturation</label><textarea id="invoice_address" name="invoice_address" rows="2" placeholder="{{ v.company_address }}">{{ v.invoice_address }}</textarea>
        <div class="hint">Ex. adresse du magasin ou du siège, si différente de l'adresse principale. Plusieurs lignes possibles.</div></div>
      <div class="field"><label for="invoice_phone">Téléphone sur la facture</label><input type="text" id="invoice_phone" name="invoice_phone" value="{{ v.invoice_phone }}" placeholder="{{ v.company_phone }}"></div>
      <div class="field"><label for="invoice_email">E-mail sur la facture</label><input type="text" id="invoice_email" name="invoice_email" value="{{ v.invoice_email }}" placeholder="{{ v.company_email }}"></div>
      <div class="field full"><label for="invoice_payment_info">Moyens de paiement acceptés</label><textarea id="invoice_payment_info" name="invoice_payment_info" rows="2" placeholder="Orange Money : 6xx xx xx xx — MTN MoMo : 6xx xx xx xx — Banque : …">{{ v.invoice_payment_info }}</textarea></div>
      <div class="field full"><label for="invoice_terms">Conditions de vente / garantie</label><textarea id="invoice_terms" name="invoice_terms" rows="2" placeholder="Garantie 6 mois sur les appareils…">{{ v.invoice_terms }}</textarea></div>
      <div class="field full"><label for="invoice_footer">Pied de facture</label><textarea id="invoice_footer" name="invoice_footer" rows="2">{{ v.invoice_footer }}</textarea></div>
      <div class="field"><label for="currency">Devise</label>
        <select id="currency" name="currency">{% for c in currencies %}<option value="{{ c }}" {{ 'selected' if c == v.currency }}>{{ c }}</option>{% endfor %}</select></div>
      <div class="field"><label for="tax_rate">Taxe sur les ventes (%)</label><input type="number" id="tax_rate" name="tax_rate" min="0" max="100" step="any" value="{{ v.tax_rate }}">
        <div class="hint">0 si vous n'appliquez pas de TVA (taux standard en Guinée : 18 %).</div></div>
      <div class="field"><label for="invoice_prefix">Préfixe des factures</label><input type="text" id="invoice_prefix" name="invoice_prefix" value="{{ v.invoice_prefix }}" maxlength="6" required></div>
      <div class="field"><label for="purchase_prefix">Préfixe des achats</label><input type="text" id="purchase_prefix" name="purchase_prefix" value="{{ v.purchase_prefix }}" maxlength="6" required></div>
      <div class="field full">
        <label class="check"><input type="checkbox" name="show_logo_on_invoice" value="1" {{ 'checked' if v.show_logo_on_invoice == '1' }}> Afficher le logo sur les factures</label>
        <label class="check" style="margin-top:.4rem"><input type="checkbox" name="low_stock_alert" value="1" {{ 'checked' if v.low_stock_alert == '1' }}> Afficher les alertes de stock bas</label>
      </div>
    </div>
  </div>
  <div class="form-actions"><button class="btn" type="submit">Enregistrer les paramètres</button></div>
</form>
{% endblock %}
''',

# ---------------------------------------------------------------- settings/logs.html
'settings/logs.html': r'''{% extends "base.html" %}
{% from "_macros.html" import pager, empty with context %}
{% block title %}Journal d'activité{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Journal d'activité</h1><p>Toutes les opérations sensibles, avec l'utilisateur et l'adresse IP.</p></div>
  <div class="actions"><a class="btn btn-light" href="{{ url_for('settings.index') }}">← Paramètres</a></div>
</div>
<form class="filters" method="get">
  <input type="search" name="action" value="{{ action }}" placeholder="Filtrer par action (ex. vente, connexion)…" aria-label="Action">
  <button class="btn" type="submit">Filtrer</button>
</form>
<div class="card flush">
  {% if logs %}
  <div class="table-wrap"><table>
    <thead><tr><th>Date</th><th>Utilisateur</th><th>Action</th><th class="hide-sm">Détails</th><th class="hide-sm">IP</th></tr></thead>
    <tbody>{% for l in logs %}
      <tr><td class="nowrap">{{ l.created_at|datetime }}</td><td>{{ l.user.full_name if l.user else '—' }}</td>
        <td><span class="badge {{ 'badge-danger' if 'echec' in l.action or 'suppression' in l.action or 'annulation' in l.action else 'badge-info' }}">{{ l.action|replace('_',' ') }}</span></td>
        <td class="hide-sm">{{ l.details }}</td><td class="hide-sm">{{ l.ip_address }}</td></tr>
    {% endfor %}</tbody>
  </table></div>
  {{ pager(pagination, 'settings.logs') }}
  {% else %}{{ empty("Aucune entrée.") }}{% endif %}
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- stock/index.html
'stock/index.html': r'''{% extends "base.html" %}
{% from "_macros.html" import pager, stock_badge, empty with context %}
{% block title %}Stocks{% endblock %}
{% block content %}
<div class="page-head">
  <div><h1>Gestion des stocks</h1><p>Valeur totale du stock : <strong>{{ total_value|money }}</strong></p></div>
  <div class="actions">
    <a class="btn btn-light" href="{{ url_for('stock.export') }}">Exporter</a>
    {% if current_user.effective_role in ('admin','gestionnaire') %}
    <a class="btn btn-light" href="{{ url_for('stock.inventory') }}">Inventaire</a>
    <a class="btn" href="{{ url_for('stock.movement') }}">＋ Mouvement</a>
    {% endif %}
  </div>
</div>

{% if alerts %}
<div class="card flush">
  <div class="card-head"><h2>⚠ Produits à réapprovisionner ({{ alerts|length }})</h2>
    {% if current_user.effective_role in ('admin','gestionnaire') %}<a href="{{ url_for('analytics.index') }}">Quantités suggérées →</a>{% endif %}</div>
  <div class="table-wrap"><table>
    <thead><tr><th>Produit</th><th class="num">Stock</th><th class="num">Minimum</th><th>État</th></tr></thead>
    <tbody>{% for p in alerts %}
      <tr><td><a href="{{ url_for('products.detail', product_id=p.id) }}">{{ p.name }}</a><span class="cell-sub">{{ p.sku }}</span></td>
        <td class="num">{{ p.quantity|num }}</td><td class="num">{{ p.min_stock|num }}</td><td>{{ stock_badge(p) }}</td></tr>
    {% endfor %}</tbody>
  </table></div>
</div>
{% endif %}

<form class="filters" method="get">
  <select name="produit" aria-label="Produit">
    <option value="">Tous les produits</option>
    {% for p in products %}<option value="{{ p.id }}" {{ 'selected' if p.id == product_id }}>{{ p.name }}</option>{% endfor %}
  </select>
  <select name="type" aria-label="Type">
    <option value="">Tous les types</option>
    {% for k, label in types.items() %}<option value="{{ k }}" {{ 'selected' if k == movement_type }}>{{ label }}</option>{% endfor %}
  </select>
  <select name="jours" aria-label="Période">
    {% for d, label in [(7,'7 derniers jours'),(30,'30 derniers jours'),(90,'3 derniers mois'),(365,'12 derniers mois')] %}
    <option value="{{ d }}" {{ 'selected' if d == days }}>{{ label }}</option>{% endfor %}
  </select>
  <button class="btn" type="submit">Filtrer</button>
</form>

<div class="card flush">
  <div class="card-head"><h2>Mouvements de stock</h2></div>
  {% if movements %}
  <div class="table-wrap"><table>
    <thead><tr><th>Date</th><th>Produit</th><th>Type</th><th class="num">Qté</th><th class="num">Stock après</th><th class="hide-sm">Motif / réf.</th><th class="hide-sm">Par</th></tr></thead>
    <tbody>{% for m in movements %}
      <tr><td class="nowrap">{{ m.created_at|datetime }}</td>
        <td><a href="{{ url_for('products.detail', product_id=m.product_id) }}">{{ m.product.name }}</a></td>
        <td>{{ m.type_label }}</td>
        <td class="num" style="color:{{ '#0b5d43' if m.quantity > 0 else '#c62f2f' }}">{{ '+' if m.quantity > 0 }}{{ m.quantity|num }}</td>
        <td class="num">{{ m.stock_after|num }}</td>
        <td class="hide-sm">{{ m.reason }}{% if m.reference %}<span class="cell-sub">{{ m.reference }}</span>{% endif %}</td>
        <td class="hide-sm">{{ m.user.full_name if m.user else '—' }}</td></tr>
    {% endfor %}</tbody>
  </table></div>
  {{ pager(pagination, 'stock.index') }}
  {% else %}{{ empty("Aucun mouvement sur cette période.") }}{% endif %}
</div>
{% endblock %}
''',

# ---------------------------------------------------------------- stock/inventory.html
'stock/inventory.html': r'''{% extends "base.html" %}
{% block title %}Inventaire{% endblock %}
{% block content %}
<div class="page-head"><div><h1>Inventaire physique</h1>
  <p>Saisissez les quantités comptées. Les champs laissés vides ne sont pas modifiés ; chaque écart crée un ajustement tracé.</p></div></div>
<form method="post" data-confirm="Valider l'inventaire et ajuster les stocks ?">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <div class="card flush">
    <div class="table-wrap"><table>
      <thead><tr><th>Produit</th><th class="num">Stock théorique</th><th style="width:160px">Quantité comptée</th></tr></thead>
      <tbody>{% for p in products %}
        <tr><td><span class="cell-title">{{ p.name }}</span><span class="cell-sub">{{ p.sku }} · {{ p.unit }}</span></td>
          <td class="num">{{ p.quantity|num }}</td>
          <td><input type="number" name="count_{{ p.id }}" min="0" step="any" inputmode="decimal" placeholder="{{ p.quantity|num }}" aria-label="Quantité comptée pour {{ p.name }}"></td></tr>
      {% else %}<tr><td colspan="3" class="empty">Aucun produit actif.</td></tr>{% endfor %}</tbody>
    </table></div>
    <div class="sticky-total">
      <a class="btn btn-light" href="{{ url_for('stock.index') }}">Annuler</a>
      <button class="btn" type="submit">Valider l'inventaire</button>
    </div>
  </div>
</form>
{% endblock %}
''',

# ---------------------------------------------------------------- stock/movement.html
'stock/movement.html': r'''{% extends "base.html" %}
{% block title %}Mouvement de stock{% endblock %}
{% block content %}
{% set v = form if form else {} %}
<div class="page-head"><div><h1>Nouveau mouvement de stock</h1>
  <p>Les achats et les ventes mettent le stock à jour automatiquement. Utilisez cette page pour les autres cas (casse, perte, don, correction…).</p></div></div>
<div class="card" style="max-width:720px">
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="form-grid">
      <div class="field full"><label for="product_id">Produit <span class="req">*</span></label>
        <select id="product_id" name="product_id" required>
          <option value="">— Choisir —</option>
          {% for p in products %}<option value="{{ p.id }}" {{ 'selected' if p.id|string == v.get('product_id','')|string }}>{{ p.name }} ({{ p.sku }}) — stock : {{ p.quantity|num }} {{ p.unit }}</option>{% endfor %}
        </select></div>
      <div class="field"><label for="movement_type">Type <span class="req">*</span></label>
        <select id="movement_type" name="movement_type">
          <option value="entree" {{ 'selected' if v.get('movement_type') == 'entree' }}>Entrée (+)</option>
          <option value="sortie" {{ 'selected' if v.get('movement_type') == 'sortie' }}>Sortie (−)</option>
          <option value="ajustement" {{ 'selected' if v.get('movement_type') == 'ajustement' }}>Ajustement (stock réel compté)</option>
        </select></div>
      <div class="field"><label for="quantity">Quantité <span class="req">*</span></label>
        <input type="number" id="quantity" name="quantity" min="0" step="any" inputmode="decimal" value="{{ v.get('quantity','') }}" required>
        <div class="hint">Pour un ajustement, saisissez la quantité réellement présente.</div></div>
      <div class="field full"><label for="reason">Motif <span class="req">*</span></label>
        <input type="text" id="reason" name="reason" value="{{ v.get('reason','') }}" maxlength="255" required list="reasons" placeholder="ex. Produit endommagé">
        <datalist id="reasons"><option value="Produit endommagé"><option value="Perte / vol"><option value="Don / échantillon"><option value="Retour client"><option value="Correction d'erreur"><option value="Transfert entre magasins"></datalist></div>
    </div>
    <div class="form-actions">
      <a class="btn btn-light" href="{{ url_for('stock.index') }}">Annuler</a>
      <button class="btn" type="submit">Enregistrer le mouvement</button>
    </div>
  </form>
</div>
{% endblock %}
''',
}


# ==================================================================================================
# STYLES, SCRIPTS ET IMAGES (fichiers statiques)
# ==================================================================================================

STATIC_FILES = {

# ---------------------------------------------------------------- css/style.css
'css/style.css': r'''/* ANSARTECH 224 — feuille de style (ordinateur, tablette, téléphone) */
:root {
  --primary: #0b5d43;
  --primary-dark: #08442f;
  --primary-soft: #e3f1ea;
  --accent: #f2b705;
  --red: #c62f2f;
  --red-soft: #fbe7e7;
  --orange: #b86e00;
  --orange-soft: #fdf1dc;
  --blue: #1d5fa8;
  --blue-soft: #e4eef9;
  --text: #17211d;
  --muted: #66736d;
  --border: #dfe5e2;
  --bg: #f4f6f5;
  --card: #ffffff;
  --radius: 12px;
  --shadow: 0 1px 2px rgba(16, 40, 30, .06), 0 4px 16px rgba(16, 40, 30, .05);
  --sidebar: 248px;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
a { color: var(--primary); text-decoration: none; }
a:hover { text-decoration: underline; }
h1, h2, h3 { margin: 0 0 .5rem; line-height: 1.25; }
h1 { font-size: 1.45rem; }
h2 { font-size: 1.1rem; }
h3 { font-size: 1rem; }
small, .muted { color: var(--muted); }
img { max-width: 100%; }

/* ---------- Structure */
.sidebar {
  position: fixed; inset: 0 auto 0 0; width: var(--sidebar); background: #0d2a20; color: #d6e4dd;
  display: flex; flex-direction: column; z-index: 40; transition: transform .25s ease;
}
.brand { display: flex; align-items: center; gap: .6rem; padding: 1.1rem 1.1rem .9rem; color: #fff; font-size: 1.1rem; letter-spacing: .02em; }
.brand:hover { text-decoration: none; }
.brand img { width: 34px; height: 34px; }
.sidebar nav { flex: 1; overflow-y: auto; padding: .25rem .6rem 1rem; }
.sidebar nav a {
  display: flex; align-items: center; gap: .65rem; padding: .55rem .7rem; border-radius: 9px;
  color: #c9d8d1; font-size: .94rem; margin-bottom: 2px;
}
.sidebar nav a i { font-style: normal; width: 1.3rem; text-align: center; opacity: .9; }
.sidebar nav a:hover { background: rgba(255, 255, 255, .07); color: #fff; text-decoration: none; }
.sidebar nav a.active { background: rgba(255, 255, 255, .12); color: #fff; font-weight: 600; }
.sidebar nav a.nav-cta { background: var(--accent); color: #2b2100; font-weight: 700; margin: .4rem 0 .2rem; }
.sidebar nav a.nav-cta:hover { background: #ffc81f; color: #2b2100; }
.nav-title { margin: 1rem .7rem .3rem; font-size: .7rem; text-transform: uppercase; letter-spacing: .09em; color: #7f978c; }
.ml { margin-left: auto; }
.sidebar-user { display: flex; align-items: center; gap: .6rem; padding: .8rem 1rem; border-top: 1px solid rgba(255, 255, 255, .08); }
.sidebar-user .who { flex: 1; min-width: 0; line-height: 1.2; }
.sidebar-user .who a { color: #fff; display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: .9rem; }
.sidebar-user small { color: #8fa79c; font-size: .75rem; }
.avatar { width: 34px; height: 34px; border-radius: 50%; background: var(--accent); color: #2b2100; display: grid; place-items: center; font-weight: 700; flex-shrink: 0; }
.avatar:hover { text-decoration: none; }
.sidebar .icon-btn { color: #c9d8d1; }

.main { margin-left: var(--sidebar); padding: 1.5rem 1.75rem 1rem; min-height: 100vh; max-width: calc(1280px + var(--sidebar)); }
.topbar { display: none; }
.overlay { display: none; }
.footer { margin-top: 2.5rem; padding: 1rem 0; color: var(--muted); font-size: .8rem; text-align: center; }

.icon-btn { background: none; border: 0; font-size: 1.25rem; padding: .35rem .5rem; cursor: pointer; color: inherit; position: relative; line-height: 1; border-radius: 8px; }
.icon-btn:hover { background: rgba(0, 0, 0, .06); text-decoration: none; }
.icon-btn .dot { position: absolute; top: -2px; right: -4px; background: var(--red); color: #fff; font-size: .65rem; border-radius: 10px; padding: 1px 5px; }

.company-chip { display: flex; align-items: center; gap: .6rem; margin: 0 .9rem .6rem; padding: .55rem .7rem; border-radius: 10px; background: rgba(255,255,255,.07); color: #fff; font-weight: 600; font-size: .9rem; line-height: 1.2; }
.company-chip img { width: 30px; height: 30px; object-fit: contain; background: #fff; border-radius: 6px; padding: 2px; }
.company-chip small { display: block; color: var(--accent); font-weight: 500; font-size: .72rem; }
.company-initial { width: 30px; height: 30px; border-radius: 6px; background: var(--accent); color: #2b2100; display: grid; place-items: center; font-weight: 800; flex-shrink: 0; }
.support-banner { display: flex; justify-content: space-between; align-items: center; gap: 1rem; flex-wrap: wrap; background: #fff4d1; border: 1px solid #f2d27a; color: #6b4b00; padding: .6rem 1rem; border-radius: 10px; margin-bottom: 1rem; }
.brand-text { max-width: 60vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.logo-preview { width: 96px; height: 96px; object-fit: contain; border: 1px solid var(--border); border-radius: 10px; background: #fff; padding: 6px; }
.swatch-row { display: flex; gap: .5rem; align-items: center; }
.swatch-row input[type=color] { width: 56px; height: 42px; padding: 2px; border: 1px solid #cbd4cf; border-radius: 9px; background: #fff; }

/* ---------- En-têtes de page */
.page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.25rem; }
.page-head p { margin: .15rem 0 0; color: var(--muted); }
.actions { display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; }

/* ---------- Cartes */
.card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); padding: 1.15rem 1.25rem; margin-bottom: 1.25rem; }
.card-head { display: flex; justify-content: space-between; align-items: center; gap: .75rem; margin-bottom: .85rem; flex-wrap: wrap; }
.card-head h2 { margin: 0; }
.card.flush { padding: 0; }
.card.flush .card-head { padding: 1rem 1.25rem 0; }

.grid { display: grid; gap: 1.25rem; }
.grid-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.grid-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.grid-main { grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); }

.kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1rem; margin-bottom: 1.25rem; }
.kpi { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 1rem 1.1rem; box-shadow: var(--shadow); border-top: 3px solid var(--primary); }
.kpi.k-accent { border-top-color: var(--accent); }
.kpi.k-blue { border-top-color: var(--blue); }
.kpi.k-red { border-top-color: var(--red); }
.kpi .label { color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; }
.kpi .value { font-size: 1.35rem; font-weight: 700; margin-top: .2rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-variant-numeric: tabular-nums; }
.kpi .sub { font-size: .8rem; color: var(--muted); }

/* ---------- Tableaux */
.table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { width: 100%; border-collapse: collapse; font-size: .92rem; }
th, td { padding: .65rem .75rem; text-align: left; border-bottom: 1px solid var(--border); vertical-align: middle; }
th { font-size: .75rem; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); font-weight: 600; background: #fafbfa; white-space: nowrap; }
tbody tr:hover { background: #f8faf9; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.nowrap { white-space: nowrap; }
tfoot td { font-weight: 700; background: #fafbfa; }
.cell-title { font-weight: 600; color: var(--text); }
.cell-sub { display: block; font-size: .8rem; color: var(--muted); }
.row-muted { opacity: .55; }

/* ---------- Badges */
.badge { display: inline-block; padding: .15rem .55rem; border-radius: 99px; font-size: .75rem; font-weight: 600; white-space: nowrap; }
.badge-success { background: var(--primary-soft); color: var(--primary-dark); }
.badge-warning { background: var(--orange-soft); color: var(--orange); }
.badge-danger { background: var(--red-soft); color: var(--red); }
.badge-info { background: var(--blue-soft); color: var(--blue); }
.badge-muted { background: #eceeed; color: #6b7570; }

/* ---------- Boutons */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: .4rem; padding: .6rem 1rem;
  border-radius: 9px; border: 1px solid transparent; font: inherit; font-weight: 600; cursor: pointer;
  background: var(--primary); color: #fff; white-space: nowrap; min-height: 42px;
}
.btn:hover { background: var(--primary-dark); text-decoration: none; }
.btn-light { background: #fff; color: var(--text); border-color: var(--border); }
.btn-light:hover { background: #f1f4f2; }
.btn-accent { background: var(--accent); color: #2b2100; }
.btn-accent:hover { background: #ffc81f; }
.btn-danger { background: var(--red); }
.btn-danger:hover { background: #a42424; }
.btn-ghost-danger { background: #fff; color: var(--red); border-color: #f0c9c9; }
.btn-ghost-danger:hover { background: var(--red-soft); }
.btn-sm { padding: .35rem .7rem; font-size: .85rem; min-height: 34px; }
.btn-block { width: 100%; }
.btn[disabled] { opacity: .5; cursor: not-allowed; }
.inline { display: inline; }

/* ---------- Formulaires */
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem 1.25rem; }
.form-grid .full { grid-column: 1 / -1; }
.field label { display: block; font-weight: 600; font-size: .88rem; margin-bottom: .3rem; }
.field .hint { font-size: .8rem; color: var(--muted); margin-top: .25rem; }
input[type=text], input[type=password], input[type=email], input[type=number], input[type=tel],
input[type=date], input[type=search], input[type=file], select, textarea {
  width: 100%; padding: .6rem .75rem; border: 1px solid #cbd4cf; border-radius: 9px; font: inherit;
  font-size: 16px; /* évite le zoom automatique sur iPhone */ background: #fff; color: var(--text); min-height: 42px;
}
textarea { min-height: 90px; resize: vertical; }
input:focus, select:focus, textarea:focus { outline: 2px solid rgba(11, 93, 67, .25); border-color: var(--primary); }
.check { display: flex; gap: .5rem; align-items: center; font-weight: 500; }
.check input { width: 18px; height: 18px; }
.form-actions { display: flex; gap: .6rem; justify-content: flex-end; margin-top: 1.25rem; flex-wrap: wrap; }
.filters { display: flex; gap: .6rem; flex-wrap: wrap; align-items: flex-end; margin-bottom: 1rem; }
.filters > * { flex: 1 1 160px; }
.filters .btn { flex: 0 0 auto; }
.req { color: var(--red); }

/* ---------- Alertes */
.flashes { margin-bottom: 1rem; }
.alert { display: flex; justify-content: space-between; gap: 1rem; align-items: flex-start; padding: .75rem 1rem; border-radius: 10px; margin-bottom: .5rem; border: 1px solid; }
.alert-success { background: var(--primary-soft); border-color: #b8dbc9; color: var(--primary-dark); }
.alert-danger { background: var(--red-soft); border-color: #f0c2c2; color: #8f1f1f; }
.alert-warning { background: var(--orange-soft); border-color: #f1d6a6; color: #7a4a00; }
.alert-info { background: var(--blue-soft); border-color: #c4d8ef; color: #164a84; }
.alert-close { background: none; border: 0; font-size: 1.2rem; cursor: pointer; color: inherit; line-height: 1; }

/* ---------- Divers */
.empty { padding: 2rem 1rem; text-align: center; color: var(--muted); }
.pager { display: flex; justify-content: center; align-items: center; gap: 1rem; padding: 1rem; color: var(--muted); font-size: .9rem; }
.list-plain { list-style: none; margin: 0; padding: 0; }
.list-plain li { display: flex; justify-content: space-between; gap: 1rem; padding: .55rem 0; border-bottom: 1px solid var(--border); }
.list-plain li:last-child { border-bottom: 0; }
.dl { display: grid; grid-template-columns: max-content 1fr; gap: .45rem 1.25rem; margin: 0; }
.dl dt { color: var(--muted); }
.dl dd { margin: 0; font-weight: 500; }
.chart-box { position: relative; height: 280px; }
.chart-box.sm { height: 230px; }
.tabs { display: flex; gap: .35rem; flex-wrap: wrap; margin-bottom: 1rem; }
.tabs a { padding: .45rem .85rem; border-radius: 99px; background: #fff; border: 1px solid var(--border); color: var(--text); font-size: .88rem; }
.tabs a.active { background: var(--primary); border-color: var(--primary); color: #fff; }
.tabs a:hover { text-decoration: none; }
.unread td { font-weight: 700; }
.message-body { white-space: pre-wrap; line-height: 1.65; }
.totals { margin-left: auto; max-width: 340px; }
.totals div { display: flex; justify-content: space-between; padding: .3rem 0; font-variant-numeric: tabular-nums; }
.totals .grand { font-size: 1.15rem; font-weight: 800; border-top: 2px solid var(--text); margin-top: .3rem; padding-top: .5rem; }
.advice { display: flex; gap: .7rem; padding: .7rem .9rem; border-radius: 10px; margin-bottom: .5rem; }
.advice::before { content: "●"; }
.advice.danger { background: var(--red-soft); color: #8f1f1f; }
.advice.warning { background: var(--orange-soft); color: #7a4a00; }
.advice.info { background: var(--blue-soft); color: #164a84; }
.advice.success { background: var(--primary-soft); color: var(--primary-dark); }

/* ---------- Saisie de vente / achat */
.line-table input { min-width: 80px; }
.line-table td { padding: .45rem .5rem; }
.line-table .product-cell { min-width: 220px; }
.search-box { position: relative; }
.search-results { position: absolute; z-index: 30; left: 0; right: 0; top: 100%; background: #fff; border: 1px solid var(--border); border-radius: 10px; box-shadow: 0 10px 30px rgba(0,0,0,.12); max-height: 320px; overflow-y: auto; margin-top: 4px; }
.search-results button { display: flex; justify-content: space-between; gap: 1rem; width: 100%; text-align: left; background: none; border: 0; border-bottom: 1px solid var(--border); padding: .7rem .85rem; font: inherit; cursor: pointer; }
.search-results button:hover, .search-results button:focus { background: var(--primary-soft); }
.search-results button[disabled] { opacity: .45; cursor: not-allowed; }
.sticky-total { position: sticky; bottom: 0; background: #fff; border-top: 1px solid var(--border); padding: .85rem 1.25rem; display: flex; justify-content: space-between; align-items: center; gap: 1rem; border-radius: 0 0 var(--radius) var(--radius); }
.sticky-total .big { font-size: 1.35rem; font-weight: 800; font-variant-numeric: tabular-nums; }

/* ---------- Connexion */
.auth-main { min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 1.5rem; background: linear-gradient(160deg, #0d2a20 0%, #0b5d43 60%, #147a59 100%); }
.auth-card { width: 100%; max-width: 400px; background: #fff; border-radius: 16px; padding: 2rem 1.75rem; box-shadow: 0 20px 60px rgba(0, 0, 0, .25); }
.auth-card .logo { display: flex; flex-direction: column; align-items: center; gap: .4rem; margin-bottom: 1.5rem; text-align: center; }
.auth-card .logo img { width: 64px; height: 64px; }
.auth-card .logo h1 { margin: 0; font-size: 1.4rem; letter-spacing: .03em; }
.auth-card .field { margin-bottom: 1rem; }
.auth-main .flashes { width: 100%; max-width: 400px; }
.auth-foot { color: rgba(255,255,255,.7); font-size: .8rem; margin-top: 1.25rem; }

.pw-wrap { position: relative; }
.pw-wrap input { padding-right: 5.5rem; }
.pw-toggle { position: absolute; right: .35rem; top: 50%; transform: translateY(-50%); background: #eef2f0; border: 0; border-radius: 7px; padding: .4rem .6rem; font: inherit; font-size: .85rem; font-weight: 600; color: var(--primary); cursor: pointer; }

/* ---------- Facture Pro */
.btn-whatsapp { background: #1fa855; color: #fff; }
.btn-whatsapp:hover { background: #178a45; }
.pro-actions { display: flex; flex-wrap: wrap; gap: .6rem; justify-content: center; align-items: center; }
.pro-actions .btn { flex: 1 1 200px; min-height: 48px; font-size: 1rem; }
.pro-actions.pulse { box-shadow: 0 0 0 3px #1fa85555; }
.pro-lines input[name="designation[]"] { min-width: 220px; }
.pv { --brand: #0b5d43; background: #fff; border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow);
      padding: 2.25rem 2.5rem; max-width: 860px; margin: 0 auto; color: #1b2420; }
.pv-head { text-align: center; }
.pv-head img { max-height: 76px; max-width: 220px; object-fit: contain; margin-bottom: .5rem; }
.pv-company { font-size: 1.6rem; font-weight: 800; color: var(--brand); line-height: 1.2; }
.pv-activity { font-style: italic; color: #5b6661; margin-top: .1rem; }
.pv-small { font-size: .85rem; color: #5b6661; margin-top: .15rem; }
.pv-rule { height: 3px; background: var(--brand); border-radius: 2px; margin: 1.1rem 0 1.4rem; }
.pv-title { text-align: center; font-size: 1.7rem; font-weight: 800; letter-spacing: .08em; }
.pv-number { text-align: center; font-weight: 700; color: var(--brand); margin-bottom: 1.4rem; }
.pv-boxes { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.4rem; }
.pv-box { background: color-mix(in srgb, var(--brand) 7%, #fff); border-left: 4px solid var(--brand); border-radius: 6px;
          padding: .8rem 1rem; display: flex; flex-direction: column; gap: .15rem; font-size: .92rem; }
.pv-box small, .pv-note small { color: var(--brand); font-weight: 800; letter-spacing: .06em; font-size: .72rem; }
.pv-box strong { font-size: 1.05rem; }
.pv-pre { white-space: pre-line; }
.pv-table { width: 100%; border-collapse: collapse; font-size: .92rem; }
.pv-table th { background: var(--brand); color: #fff; font-size: .74rem; letter-spacing: .05em; }
.pv-table tbody tr:nth-child(even) { background: color-mix(in srgb, var(--brand) 7%, #fff); }
.pv-table tbody tr:last-child td { border-bottom: 2px solid var(--brand); }
.pv-idx { color: #5b6661; text-align: center; width: 40px; }
.pv-totals { margin: 1.1rem 0 0 auto; max-width: 340px; }
.pv-totals div { display: flex; justify-content: space-between; padding: .4rem .7rem; border-bottom: 1px solid #e6ebe8; font-variant-numeric: tabular-nums; }
.pv-totals .pv-grand { background: var(--brand); color: #fff; font-weight: 800; font-size: 1.1rem; border-radius: 4px; border: 0; }
.pv-totals .pv-due { color: var(--brand); font-weight: 800; background: color-mix(in srgb, var(--brand) 14%, #fff); }
.pv-words { text-align: center; margin: 1.4rem 0 1rem; padding: .9rem 1rem; background: color-mix(in srgb, var(--brand) 7%, #fff);
            border: 1px solid color-mix(in srgb, var(--brand) 18%, #fff); border-radius: 6px; font-size: .95rem; }
.pv-note { border-left: 3px solid var(--brand); padding: .25rem .8rem; margin-top: .8rem; display: flex; flex-direction: column; font-size: .9rem; }
.pv-signs { display: flex; justify-content: space-between; gap: 2rem; margin-top: 2rem; font-size: .9rem; color: #5b6661; }
.pv-signs div { flex: 0 1 240px; text-align: center; }
.pv-signs span { display: block; margin-top: 3.2rem; padding-top: .35rem; border-top: 1px solid #9aa5a0; }
.pv-footer { text-align: center; color: #5b6661; font-size: .8rem; margin-top: 1.6rem; padding-top: .7rem; border-top: 2px solid var(--brand); }
@media (max-width: 860px) {
  .pv { padding: 1.25rem 1rem; }
  .pv-boxes { grid-template-columns: 1fr; }
  .pv-company { font-size: 1.3rem; }
  .pv-title { font-size: 1.35rem; }
  .pv-totals { max-width: none; }
  .pro-lines input[name="designation[]"] { min-width: 0; }
  .pro-actions .btn { flex-basis: 100%; }
}

/* ---------- Erreurs */
.error-page { text-align: center; padding: 4rem 1rem; }
.error-page .code { font-size: 4rem; font-weight: 800; color: var(--primary); }

/* ---------- Tablette */
@media (max-width: 1100px) {
  .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .grid-3 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .grid-main { grid-template-columns: 1fr; }
}

/* ---------- Téléphone */
@media (max-width: 860px) {
  .sidebar { transform: translateX(-100%); box-shadow: 0 0 40px rgba(0,0,0,.3); }
  body.menu-open .sidebar { transform: none; }
  body.menu-open .overlay { display: block; position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 35; }
  body.menu-open { overflow: hidden; }
  .topbar {
    display: flex; position: sticky; top: 0; z-index: 30; align-items: center; justify-content: space-between;
    background: var(--primary); color: #fff; padding: .55rem .75rem; padding-top: max(.55rem, env(safe-area-inset-top));
  }
  .topbar .icon-btn { color: #fff; }
  .topbar .icon-btn:hover { background: rgba(255,255,255,.12); }
  .brand-sm { display: flex; align-items: center; gap: .5rem; color: #fff; font-weight: 700; letter-spacing: .03em; }
  .brand-sm img { width: 26px; height: 26px; object-fit: contain; background: #fff; border-radius: 5px; }
  .main { margin-left: 0; padding: 1rem .85rem; }
  .grid-2, .grid-3, .form-grid { grid-template-columns: 1fr; }
  .card { padding: 1rem; }
  .card.flush .card-head { padding: 1rem 1rem 0; }
  .page-head { flex-direction: column; align-items: stretch; }
  .page-head .actions .btn { flex: 1 1 auto; }
  h1 { font-size: 1.25rem; }
  .kpi .value { font-size: 1.05rem; white-space: normal; overflow-wrap: anywhere; }
  .kpi { padding: .85rem; }
  .hide-sm { display: none; }
  th, td { padding: .55rem .6rem; }
  .form-actions .btn { flex: 1 1 auto; }
  .totals { max-width: none; }
  .chart-box { height: 230px; }
}
@media (max-width: 860px) {
  .line-table thead { display: none; }
  .line-table, .line-table tbody { display: block; }
  .line-table tr.line { display: grid; grid-template-columns: 1fr 1fr auto; gap: .5rem .6rem; align-items: end; padding: .8rem 1rem; border-bottom: 1px solid var(--border); }
  .line-table tr.line td { padding: 0; border: 0; }
  .line-table td.product-cell { grid-column: 1 / -1; min-width: 0; }
  .line-table td[data-label]::before { content: attr(data-label); display: block; font-size: .75rem; color: var(--muted); margin-bottom: .2rem; }
  .line-table td.lt { grid-column: 1 / -1; text-align: right; font-weight: 700; }
  .line-table td.lt::before { display: inline; margin-right: .5rem; }
  .line-table input { min-width: 0; }
  .line-table tr.line td:last-child { grid-row: 2; grid-column: 3; }
}
@media (max-width: 380px) {
  .kpis { grid-template-columns: 1fr; }
}
''',

# ---------------------------------------------------------------- js/app.js
'js/app.js': r'''/* ANSARTECH 224 — scripts de l'interface */
(function () {
  "use strict";

  // ---------- Menu mobile
  var toggle = document.getElementById("menuToggle");
  var overlay = document.getElementById("overlay");
  if (toggle) toggle.addEventListener("click", function () { document.body.classList.toggle("menu-open"); });
  if (overlay) overlay.addEventListener("click", function () { document.body.classList.remove("menu-open"); });

  // ---------- Fermeture des alertes
  document.addEventListener("click", function (e) {
    if (e.target.classList.contains("alert-close")) e.target.parentElement.remove();
  });

  // ---------- Confirmation avant action sensible
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) { e.preventDefault(); return; }
    var btn = form.querySelector("button[type=submit], button:not([type])");
    if (btn && !form.hasAttribute("data-no-lock")) {
      setTimeout(function () { btn.disabled = true; }, 0); // évite les doubles envois
    }
  });

  // ---------- Formatage monétaire
  function fmt(n) {
    n = Math.round(Number(n) || 0);
    return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }
  function num(v) {
    var n = parseFloat(String(v || "").replace(/\s/g, "").replace(",", "."));
    return isNaN(n) ? 0 : n;
  }
  window.ansarFmt = fmt;

  // ---------- Éditeur de lignes (ventes et achats)
  var editor = document.querySelector("[data-line-editor]");
  if (!editor) return;

  var mode = editor.getAttribute("data-line-editor"); // "sale" ou "purchase"
  var searchUrl = editor.getAttribute("data-search-url");
  var currency = editor.getAttribute("data-currency") || "";
  var taxRate = num(editor.getAttribute("data-tax-rate"));
  var input = document.getElementById("productSearch");
  var results = document.getElementById("searchResults");
  var tbody = document.getElementById("lines");
  var emptyRow = document.getElementById("emptyLines");
  var timer = null;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function recalc() {
    var subtotal = 0;
    var rows = tbody.querySelectorAll("tr.line");
    rows.forEach(function (row) {
      var q = num(row.querySelector(".q").value);
      var p = num(row.querySelector(".p").value);
      var total = q * p;
      subtotal += total;
      row.querySelector(".lt").textContent = fmt(total);
      var warn = row.querySelector(".stock-warn");
      if (warn) {
        var stock = num(row.getAttribute("data-stock"));
        warn.hidden = !(mode === "sale" && q > stock);
      }
    });
    if (emptyRow) emptyRow.hidden = rows.length > 0;
    var discountEl = document.getElementById("discount");
    var discount = discountEl ? num(discountEl.value) : 0;
    var base = Math.max(subtotal - discount, 0);
    var tax = mode === "sale" ? base * taxRate / 100 : 0;
    var total = base + tax;
    setText("subtotal", fmt(subtotal) + " " + currency);
    setText("taxAmount", fmt(tax) + " " + currency);
    setText("grandTotal", fmt(total) + " " + currency);
    var paid = document.getElementById("amount_paid");
    var changeEl = document.getElementById("changeInfo");
    if (paid && changeEl) {
      var p = paid.value.trim() === "" ? total : num(paid.value);
      if (p > total) changeEl.textContent = "Monnaie à rendre : " + fmt(p - total) + " " + currency;
      else if (p < total) changeEl.textContent = "Reste à payer : " + fmt(total - p) + " " + currency;
      else changeEl.textContent = "Paiement complet";
    }
  }
  function setText(id, text) { var el = document.getElementById(id); if (el) el.textContent = text; }

  function addLine(item) {
    var existing = tbody.querySelector('tr.line[data-id="' + item.id + '"]');
    if (existing) {
      var q = existing.querySelector(".q");
      q.value = num(q.value) + 1;
      recalc();
      return;
    }
    var price = item.price != null ? item.price : (mode === "sale" ? item.sale : item.cost);
    var tr = document.createElement("tr");
    tr.className = "line";
    tr.setAttribute("data-id", item.id);
    tr.setAttribute("data-stock", item.stock);
    tr.innerHTML =
      '<td class="product-cell"><input type="hidden" name="product_id[]" value="' + item.id + '">' +
      '<span class="cell-title">' + escapeHtml(item.name) + '</span>' +
      '<span class="cell-sub">' + escapeHtml(item.sku) + ' · stock : ' + fmt(item.stock) + ' ' + escapeHtml(item.unit || "") + '</span>' +
      '<span class="cell-sub stock-warn" style="color:#c62f2f" hidden>Stock insuffisant</span></td>' +
      '<td data-label="Quantité"><input class="q" type="number" name="quantity[]" min="0.001" step="any" inputmode="decimal" value="' + (item.qty || 1) + '" required></td>' +
      '<td data-label="Prix"><input class="p" type="number" name="price[]" min="0" step="any" inputmode="decimal" value="' + (price || 0) + '" required></td>' +
      '<td class="num lt" data-label="Total">0</td>' +
      '<td><button type="button" class="btn btn-ghost-danger btn-sm rm" aria-label="Retirer">✕</button></td>';
    tbody.appendChild(tr);
    recalc();
  }

  function search(q) {
    fetch(searchUrl + "?q=" + encodeURIComponent(q), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.results.length) {
          results.innerHTML = '<div class="empty">Aucun produit trouvé</div>';
        } else {
          results.innerHTML = data.results.map(function (p) {
            var out = mode === "sale" && p.stock <= 0;
            return '<button type="button" data-item=\'' + escapeHtml(JSON.stringify({
              id: p.id, name: p.name, sku: p.sku, stock: p.stock, unit: p.unit,
              price: mode === "sale" ? p.price : p.cost })) + '\'' + (out ? " disabled" : "") + '>' +
              '<span><strong>' + escapeHtml(p.name) + '</strong><span class="cell-sub">' + escapeHtml(p.sku) +
              ' · stock ' + fmt(p.stock) + (out ? " (rupture)" : "") + '</span></span>' +
              '<span class="nowrap">' + fmt(mode === "sale" ? p.price : p.cost) + '</span></button>';
          }).join("");
        }
        results.hidden = false;
      })
      .catch(function () { results.innerHTML = '<div class="empty">Erreur de connexion</div>'; results.hidden = false; });
  }

  input.addEventListener("input", function () {
    clearTimeout(timer);
    var q = input.value.trim();
    timer = setTimeout(function () { search(q); }, 200);
  });
  input.addEventListener("focus", function () { search(input.value.trim()); });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault(); // lecteur code-barres : ajoute le premier résultat
      var first = results.querySelector("button:not([disabled])");
      if (first) first.click();
    }
  });
  results.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-item]");
    if (!btn) return;
    addLine(JSON.parse(btn.getAttribute("data-item")));
    results.hidden = true;
    input.value = "";
    input.focus();
  });
  document.addEventListener("click", function (e) {
    if (!e.target.closest(".search-box")) results.hidden = true;
  });
  tbody.addEventListener("input", recalc);
  tbody.addEventListener("click", function (e) {
    if (e.target.classList.contains("rm")) { e.target.closest("tr").remove(); recalc(); }
  });
  ["discount", "amount_paid"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener("input", recalc);
  });

  editor.closest("form").addEventListener("submit", function (e) {
    if (!tbody.querySelector("tr.line")) {
      e.preventDefault();
      e.stopImmediatePropagation();
      alert("Ajoutez au moins un produit.");
      input.focus();
    }
  }, true);

  // lignes à restaurer après une erreur de validation
  try {
    JSON.parse(editor.getAttribute("data-initial") || "[]").forEach(addLine);
  } catch (err) { /* rien */ }
  recalc();
})();
''',

# ---------------------------------------------------------------- img/logo.svg
'img/logo.svg': r'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="14" fill="#0b5d43"/>
  <rect x="0" y="52" width="21.3" height="12" fill="#ce1126"/>
  <rect x="21.3" y="52" width="21.4" height="12" fill="#fcd116"/>
  <rect x="42.7" y="52" width="21.3" height="12" fill="#009460"/>
  <path d="M32 9 L49 45 H41.5 L38 37 H26 L22.5 45 H15 Z M28.6 31 H35.4 L32 23 Z" fill="#fff" fill-rule="evenodd"/>
</svg>
''',

# ---------------------------------------------------------------- manifest.json
'manifest.json': r'''{
  "name": "ANSARTECH 224",
  "short_name": "ANSARTECH",
  "description": "Gestion commerciale : stocks, achats, ventes et facturation",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#f4f6f5",
  "theme_color": "#0b5d43",
  "lang": "fr",
  "icons": [
    { "src": "/static/img/logo.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any" }
  ]
}
''',
}


STATIC_TYPES = {"css": "text/css; charset=utf-8", "js": "application/javascript; charset=utf-8",
                "svg": "image/svg+xml", "json": "application/manifest+json"}
CHART_JS_CDN = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"


def serve_static(filename):
    """Sert les fichiers statiques intégrés à ce fichier."""
    if filename == "js/chart.umd.min.js":
        return redirect(CHART_JS_CDN)
    content = STATIC_FILES.get(filename)
    if content is None:
        abort(404)
    ext = filename.rsplit(".", 1)[-1]
    return Response(content, content_type=STATIC_TYPES.get(ext, "application/octet-stream"),
                    headers={"Cache-Control": "public, max-age=86400"})


# ==================================================================================================
# DÉMARRAGE
# ==================================================================================================

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
