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
# ==============
