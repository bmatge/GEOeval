"""
Routes d'authentification applicative (ADR-086) : login/logout locaux,
landing d'invitation avec création de compte, liens de définition de mot de
passe, et flux OIDC optionnel.

Séparées de `webapp/app.py` : ces pages se rendent sans organisation (nav
masquée) et doivent rester accessibles à un visiteur anonyme.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from webapp import accounts, audit, oidc, tenancy
from webapp.auth import SESSION_USER_KEY, CurrentUser
from webapp.deps import get_db

logger = logging.getLogger("webapp.auth_routes")

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _render(request: Request, template: str, **ctx) -> HTMLResponse:
    """Rendu minimal hors organisation (nav masquée, base.html satisfait)."""
    context = {
        "active": "auth",
        "user": getattr(request.state, "user", None),
        "org": None,
        "role": None,
        "url_prefix": "",
        "oidc_enabled": oidc.oidc_enabled(),
        "oidc_label": oidc.provider_label(),
        **ctx,
    }
    return templates.TemplateResponse(request=request, name=template, context=context)


def _safe_next(next_url: Optional[str]) -> str:
    """Anti open-redirect : seules les URLs relatives internes sont suivies."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


def _login_session(request: Request, user_id: int) -> None:
    request.session.clear()
    request.session[SESSION_USER_KEY] = user_id


# =====================================================================
# Login / logout locaux
# =====================================================================
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str = ""):
    user: Optional[CurrentUser] = getattr(request.state, "user", None)
    if user is not None:
        return RedirectResponse(_safe_next(next), status_code=302)
    return _render(request, "login.html", next=_safe_next(next), error=error)


@router.post("/login")
def login_submit(
    request: Request,
    db: Session = Depends(get_db),
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    user = accounts.authenticate(db, email=email, password=password)
    if user is None:
        return _render(
            request,
            "login.html",
            next=_safe_next(next),
            error="Identifiants invalides.",
            email=email,
        )
    _login_session(request, user.id)
    audit.record(
        db, user_id=user.id, org_id=None, action="login",
        entity_type="auth", meta={"provider": "local"},
    )
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    user: Optional[CurrentUser] = getattr(request.state, "user", None)
    if user is not None:
        audit.record(
            db, user_id=user.id, org_id=None, action="logout", entity_type="auth",
        )
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# =====================================================================
# Invitations — landing publique + création de compte (ADR-086 §1)
# =====================================================================
def _invitation_or_none(db: Session, token: str):
    inv = tenancy.get_invitation_by_token(db, token)
    if inv is None or tenancy.invitation_is_expired(inv):
        return None
    return inv


@router.get("/invitations/{token}", response_class=HTMLResponse)
def invitation_landing(token: str, request: Request, db: Session = Depends(get_db)):
    inv = _invitation_or_none(db, token)
    if inv is None:
        return _render(request, "invitation_landing.html", state="invalid")
    org = tenancy.get_org(db, inv.org_id)
    user: Optional[CurrentUser] = getattr(request.state, "user", None)

    if user is not None:
        if user.email == inv.email:
            # Déjà connecté avec le bon email : page de confirmation existante.
            return RedirectResponse(
                f"/o/{org.slug}/accept-invite?token={token}", status_code=302
            )
        return _render(
            request, "invitation_landing.html",
            state="wrong_account", org_name=org.name, invited_email=inv.email,
        )

    if accounts.get_user_by_email(db, inv.email) is not None:
        # Compte existant : se connecter puis revenir ici.
        return _render(
            request, "invitation_landing.html",
            state="login_required", org_name=org.name, invited_email=inv.email,
            next=f"/invitations/{token}",
        )

    # Email inconnu : proposer la création du compte.
    return _render(
        request, "invitation_landing.html",
        state="signup", org_name=org.name, invited_email=inv.email, token=token,
    )


@router.post("/invitations/{token}/signup")
def invitation_signup(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    inv = _invitation_or_none(db, token)
    if inv is None:
        raise HTTPException(status_code=403, detail="Invitation invalide ou expirée.")
    if accounts.get_user_by_email(db, inv.email) is not None:
        raise HTTPException(status_code=409, detail="Un compte existe déjà pour cet email.")
    org = tenancy.get_org(db, inv.org_id)

    err = accounts.validate_password_strength(password)
    if err is None and password != password_confirm:
        err = "Les deux mots de passe ne correspondent pas."
    if err:
        return _render(
            request, "invitation_landing.html",
            state="signup", org_name=org.name, invited_email=inv.email,
            token=token, error=err,
        )

    user = accounts.create_local_user(db, email=inv.email, password=password)
    m = tenancy.accept_invitation(db, token=token, current_email=user.email)
    _login_session(request, user.id)
    audit.record(
        db, user_id=user.id, org_id=inv.org_id, action="signup",
        entity_type="auth", meta={"via": "invitation", "role": m.role},
    )
    return RedirectResponse(f"/o/{org.slug}/", status_code=303)


# =====================================================================
# Définition / reset de mot de passe par lien one-shot (sans SMTP)
# =====================================================================
@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_form(token: str, request: Request, db: Session = Depends(get_db)):
    tok = accounts.get_valid_token(db, token)
    if tok is None:
        return _render(request, "set_password.html", state="invalid")
    from models import User

    user = db.get(User, tok.user_id)
    return _render(request, "set_password.html", state="form", token=token, email=user.email)


@router.post("/reset/{token}")
def reset_submit(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    tok = accounts.get_valid_token(db, token)
    if tok is None:
        return _render(request, "set_password.html", state="invalid")
    from models import User

    user = db.get(User, tok.user_id)
    err = accounts.validate_password_strength(password)
    if err is None and password != password_confirm:
        err = "Les deux mots de passe ne correspondent pas."
    if err:
        return _render(
            request, "set_password.html", state="form",
            token=token, email=user.email, error=err,
        )

    accounts.set_password(db, user_id=user.id, password=password)
    accounts.consume_token(db, tok)
    _login_session(request, user.id)
    audit.record(
        db, user_id=user.id, org_id=None, action="password_set", entity_type="auth",
    )
    return RedirectResponse("/", status_code=303)


# =====================================================================
# OIDC optionnel (ADR-086 §3) — flux authorization-code + PKCE
# =====================================================================
@router.get("/auth/oidc/login")
async def oidc_login(request: Request):
    if not oidc.oidc_enabled():
        raise HTTPException(status_code=404, detail="SSO non configuré.")
    client = oidc.get_client()
    redirect_uri = str(request.url_for("oidc_callback"))
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/auth/oidc/callback", name="oidc_callback")
async def oidc_callback(request: Request, db: Session = Depends(get_db)):
    if not oidc.oidc_enabled():
        raise HTTPException(status_code=404, detail="SSO non configuré.")
    from authlib.integrations.base_client.errors import OAuthError

    client = oidc.get_client()
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as e:
        logger.warning("callback OIDC en échec : %s", e)
        return _render(request, "login.html", next="/", error=f"Connexion SSO refusée ({e.error}).")

    claims = dict(token.get("userinfo") or {})
    sub = claims.get("sub")
    email = (claims.get("email") or "").strip().lower()
    email_verified = bool(claims.get("email_verified"))
    if not sub:
        return _render(request, "login.html", next="/", error="Réponse SSO sans identifiant (claim sub).")

    from sqlalchemy import select
    from models import User

    issuer = oidc.issuer()
    user = db.execute(
        select(User).where(User.oidc_issuer == issuer, User.oidc_external_id == sub)
    ).scalar_one_or_none()

    if user is None:
        # Réconciliation par email — uniquement si l'IdP atteste l'adresse
        # (anti-takeover, ADR-061 §2). Sinon refus explicite.
        if not email or not email_verified:
            audit.record(
                db, user_id=None, org_id=None, action="oidc_rejected",
                entity_type="auth", meta={"sub": sub, "email": email or None,
                                          "reason": "email absent ou non vérifié"},
            )
            return _render(
                request, "login.html", next="/",
                error="Connexion SSO refusée : email absent ou non vérifié par le fournisseur d'identité.",
            )
        user = accounts.get_user_by_email(db, email)
        if user is None:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            user = User(
                email=email, first_seen_at=now, last_seen_at=now,
                auth_provider="oidc", oidc_issuer=issuer, oidc_external_id=sub,
            )
            db.add(user)
        else:
            user.oidc_issuer = issuer
            user.oidc_external_id = sub
        db.commit()

    if oidc.claims_admin(claims) and not user.is_platform_admin:
        # Promotion uniquement (jamais de rétrogradation automatique).
        user.is_platform_admin = True
        db.commit()

    _login_session(request, user.id)
    audit.record(
        db, user_id=user.id, org_id=None, action="login",
        entity_type="auth", meta={"provider": "oidc", "issuer": issuer},
    )
    return RedirectResponse("/", status_code=303)
