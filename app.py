#!/usr/bin/env python3
"""Thing To Sell To Ebay - Ecwid app.

Custom Ecwid app that replaces M2E Multichannel for eBay integration.
Features:
- OAuth installation flow for Ecwid
- Product selection UI (choose which products to publish to eBay)
- One-click publish to eBay via Inventory API
- Sync dashboard (inventory, orders, prices)
- Settings (eBay policy IDs, location)

Environment variables:
    ECWID_CLIENT_ID       OAuth client ID from Ecwid developer portal
    ECWID_CLIENT_SECRET   OAuth client secret
    APP_URL               Public HTTPS URL of this app (e.g. https://xxx.onrender.com)
    EBAY_*                eBay credentials handled via secure vault (not needed here,
                          the sync scripts use authd surrogates)

    For local dev, tokens are read from the same secure connectors the CLI uses.
"""
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

from flask import Flask, request, redirect, url_for, render_template, jsonify, session


def read_json_response(response):
    """Read and parse JSON from a urllib response."""
    return json.loads(response.read().decode("utf-8"))

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))

@app.context_processor
def inject_lang():
    return {"lang": session.get("lang", "es")}

@app.route("/lang/<lang>")
def set_lang(lang):
    session["lang"] = "en" if lang == "en" else "es"
    next_url = request.args.get("next", "/dashboard")
    store_id = request.args.get("store_id", "")
    if store_id and "store_id" not in next_url:
        sep = "&" if "?" in next_url else "?"
        next_url = f"{next_url}{sep}store_id={store_id}"
    return redirect(next_url)

# Config from environment
ECWID_CLIENT_ID = os.environ.get("ECWID_CLIENT_ID", "")
ECWID_CLIENT_SECRET = os.environ.get("ECWID_CLIENT_SECRET", "")
APP_URL = os.environ.get("APP_URL", "").rstrip("/")
ECWID_API_BASE = "https://app.ecwid.com/api/v3"
ECWID_OAUTH_BASE = "https://my.ecwid.com/api/oauth"

# Default store (Gelson's) - overridden per-installation via OAuth
DEFAULT_STORE_ID = "120529764"

# eBay defaults for car stereos (configurable in Settings)
DEFAULT_EBAY_CONFIG = {
    "payment_policy_id": "274761426019",
    "payment_policy_name": "Thing To Sell LLC Payment Policy",
    "fulfillment_policy_id": "279114158019",
    "fulfillment_policy_name": "Thing To Sell LLC Shipping Policy - Free Shipping Included",
    "return_policy_id": "275479123019",
    "return_policy_name": "Thing To Sell LLC Return Policy",
    "return_policy_30day_id": "275104384019",
    "return_policy_30day_name": "30-Day Return Policy",
    "merchant_location_key": "dallas-tx",
    "category_id": "94845",
}

DEFAULT_TEMPLATES = {
    "selling": [{"name": "Default", "id": "default"}],
    "synchronization": [{"name": "Default", "id": "default"}],
    "description": [{"name": "Brand New Open Box.", "id": "openbox",
                     "text": "Brand New Open Box"}],
}

# Simple file-based store for app settings (per Ecwid store)
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)


def _store_file(store_id):
    return os.path.join(DATA_DIR, f"store_{store_id}.json")


def load_store_config(store_id):
    """Load per-store config (eBay policies, published products)."""
    path = _store_file(store_id)
    config = {
        "ebay": dict(DEFAULT_EBAY_CONFIG),
        "templates": {k: list(v) for k, v in DEFAULT_TEMPLATES.items()},
        "published": {},  # ecwid_product_id -> {ebay_sku, offer_id, listing_id}
        "ecwid_token": None,
    }
    if os.path.exists(path):
        try:
            with open(path) as f:
                saved = json.load(f)
            config.update(saved)
            # Merge ebay defaults so new keys appear
            merged = dict(DEFAULT_EBAY_CONFIG)
            merged.update(saved.get("ebay", {}))
            config["ebay"] = merged
        except (ValueError, OSError):
            pass
    return config


def save_store_config(store_id, config):
    path = _store_file(store_id)
    # Never persist raw tokens to disk in plain text alongside other config
    # if we can avoid it; for this private single-store app it's acceptable,
    # file has restricted permissions.
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# Ecwid API helpers (per-store OAuth token)
# ---------------------------------------------------------------------------

def ecwid_api(store_id, method, path, data=None, token=None):
    """Call Ecwid REST API for a store."""
    if token is None:
        config = load_store_config(store_id)
        token = config.get("ecwid_token")
    if not token:
        # Fallback: use the app's own API token from env (custom single-store app)
        token = os.environ.get("ECWID_API_TOKEN", "") or os.environ.get("ECWID_CLIENT_SECRET", "")
    if not token:
        raise Exception("Tienda no conectada. Reinstala la app.")
    url = f"{ECWID_API_BASE}/{store_id}{path}"
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise Exception(f"Ecwid API {e.code}: {e.read().decode()[:300]}")


def ecwid_products(store_id, limit=100, offset=0, token=None):
    return ecwid_api(store_id, "GET", f"/products?limit={limit}&offset={offset}", token=token)


# ---------------------------------------------------------------------------
# eBay API helpers (OAuth2 with refresh token from environment)
# ---------------------------------------------------------------------------

EBAY_API = "https://api.ebay.com"
EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"

# In-memory token cache
_ebay_token_cache = {"access_token": None, "expires_at": 0}


def ebay_get_access_token():
    """Get a valid eBay access token, refreshing if needed."""
    now = time.time()
    if _ebay_token_cache["access_token"] and _ebay_token_cache["expires_at"] > now + 60:
        return _ebay_token_cache["access_token"]

    client_id = os.environ.get("EBAY_CLIENT_ID", "")
    client_secret = os.environ.get("EBAY_CLIENT_SECRET", "")
    refresh_token = os.environ.get("EBAY_REFRESH_TOKEN", "")

    if not all([client_id, client_secret, refresh_token]):
        raise Exception("eBay credentials not configured (EBAY_CLIENT_ID/EBAY_CLIENT_SECRET/EBAY_REFRESH_TOKEN)")

    import base64
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "https://api.ebay.com/oauth/api_scope/sell.inventory https://api.ebay.com/oauth/api_scope/sell.fulfillment",
    }).encode()

    req = urllib.request.Request(EBAY_TOKEN_URL, data=data, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {creds}",
    })
    resp = read_json_response(urllib.request.urlopen(req, timeout=30))
    _ebay_token_cache["access_token"] = resp["access_token"]
    _ebay_token_cache["expires_at"] = now + int(resp.get("expires_in", 7200))
    return _ebay_token_cache["access_token"]


def ebay_api(method, path, data=None):
    url = EBAY_API + path
    body = json.dumps(data).encode() if data is not None else None
    headers = {
        "Content-Language": "en-US",
        "Authorization": f"Bearer {ebay_get_access_token()}",
    }
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        return read_json_response(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        raise Exception(f"eBay API {e.code}: {e.read().decode()[:500]}")


def ebay_publish_product(ecwid_product, ebay_cfg, overrides=None):
    """Create eBay inventory item + offer + publish. Returns listing info.

    ecwid_product: dict with id, name, price, quantity, description, etc.
    overrides: optional dict with title, price, quantity, condition, category_id
    """
    ov = overrides or {}
    ecwid_id = str(ecwid_product["id"])
    sku = f"ECWID-{ecwid_id}"
    price = float(ov.get("price", ecwid_product.get("price", 0)))
    qty = int(ov.get("quantity", ecwid_product.get("quantity", 0)) or 0)
    title = ov.get("title", ecwid_product.get("name", ""))[:80]
    condition = ov.get("condition", "NEW")
    category_id = ov.get("category_id", ebay_cfg.get("category_id", "94845"))

    # 1. Create/update inventory item
    item = {
        "product": {
            "title": title,
            "description": ecwid_product.get("description", ""),
            "aspects": {
                "Brand": ["Unbranded"],
                "MPN": ["Does Not Apply"],
            },
            "upc": ["Does Not Apply"],
            "imageUrls": [],
        },
        "condition": condition,
        "packageWeightAndSize": {
            "weight": {"value": 4, "unit": "POUND"},
            "dimensions": {"height": 4, "length": 4, "width": 4, "unit": "INCH"},
        },
        "availability": {
            "shipToLocationAvailability": {"quantity": qty}
        },
    }
    # Add product images if available
    img = ecwid_product.get("imageUrl") or ecwid_product.get("thumbnailUrl")
    if img:
        item["product"]["imageUrls"] = [img]

    ebay_api("PUT", f"/sell/inventory/v1/inventory_item/{urllib.parse.quote(sku, safe='')}", item)

    # 2. Create offer with listingPolicies (NOT top-level!)
    offer = {
        "sku": sku,
        "marketplaceId": "EBAY_US",
        "format": "FIXED_PRICE",
        "availableQuantity": qty,
        "categoryId": category_id,
        "listingDuration": "GTC",
        "listingDescription": ecwid_product.get("description", "")[:8000],
        "pricingSummary": {"price": {"value": str(price), "currency": "USD"}},
        "listingPolicies": {
            "fulfillmentPolicyId": ov.get("fulfillment_policy_id", ebay_cfg["fulfillment_policy_id"]),
            "paymentPolicyId": ov.get("payment_policy_id", ebay_cfg["payment_policy_id"]),
            "returnPolicyId": ov.get("return_policy_id", ebay_cfg["return_policy_id"]),
        },
        "merchantLocationKey": ebay_cfg.get("merchant_location_key", "dallas-tx"),
        "tax": {"applyTax": False},
    }
    created = ebay_api("POST", "/sell/inventory/v1/offer", offer)
    offer_id = created.get("offerId")

    # 3. Publish
    published = ebay_api("POST", f"/sell/inventory/v1/offer/{offer_id}/publish", {})
    return {
        "ebay_sku": sku,
        "offer_id": offer_id,
        "listing_id": published.get("listingId"),
    }


# ---------------------------------------------------------------------------
# Routes: OAuth install flow
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("dashboard"))


@app.route("/oauth/callback")
def oauth_callback():
    """Ecwid redirects here after merchant authorizes the app."""
    code = request.args.get("code")
    if not code:
        return "Falta el código de autorización.", 400
    # Exchange code for token
    data = urllib.parse.urlencode({
        "client_id": ECWID_CLIENT_ID,
        "client_secret": ECWID_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": f"{APP_URL}/oauth/callback",
    }).encode()
    req = urllib.request.Request(
        f"{ECWID_OAUTH_BASE}/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            token_data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return f"Error de OAuth: {e.read().decode()[:300]}", 400

    store_id = str(token_data.get("store_id", DEFAULT_STORE_ID))
    access_token = token_data.get("access_token")

    config = load_store_config(store_id)
    config["ecwid_token"] = access_token
    save_store_config(store_id, config)

    session["store_id"] = store_id
    return redirect(url_for("dashboard", store_id=store_id))


# ---------------------------------------------------------------------------
# Routes: main UI
# ---------------------------------------------------------------------------

def _current_store():
    return request.args.get("store_id") or session.get("store_id") or DEFAULT_STORE_ID


@app.route("/dashboard")
def dashboard():
    store_id = _current_store()
    config = load_store_config(store_id)
    published = config.get("published", {})

    # Load sync state if available
    sync_state = {}
    state_path = os.path.expanduser(
        "~/workspace/goals/custom-ecwid-to-ebay-integration/hidden_files/"
        "ebay-integration/inventory_sync_state.json"
    )
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                sync_state = json.load(f)
        except (ValueError, OSError):
            pass

    # Total products from Ecwid
    total_products = 0
    try:
        data = ecwid_api(store_id, "GET", "/products?limit=1&offset=0")
        total_products = data.get("total", 0)
    except Exception:
        pass

    listed_count = len(published)
    not_listed_count = max(0, total_products - listed_count)
    listed_pct = round(listed_count / total_products * 100, 2) if total_products else 0
    not_listed_pct = round(100 - listed_pct, 2) if total_products else 0

    # Setup guide steps
    has_linked = listed_count > 0
    has_sync = bool(sync_state.get("last_run") or sync_state.get("ecwid_quantities"))
    has_orders = bool(sync_state.get("processed_orders"))
    steps_completed = sum([has_linked, has_sync, has_orders])
    # List New Items is complete only if everything is listed
    list_new_done = total_products > 0 and not_listed_count == 0
    if list_new_done:
        steps_completed += 1

    # Errors
    listing_errors = sync_state.get("listing_errors", [])
    order_errors = sync_state.get("order_errors", [])
    last_errors = sync_state.get("last_errors")
    if last_errors and isinstance(last_errors, list):
        listing_errors = listing_errors + last_errors

    return render_template(
        "dashboard.html",
        store_id=store_id,
        published=published,
        published_count=listed_count,
        sync_state=sync_state,
        ebay_config=config.get("ebay", {}),
        total_products=total_products,
        listed_count=listed_count,
        not_listed_count=not_listed_count,
        listed_pct=listed_pct,
        not_listed_pct=not_listed_pct,
        has_linked=has_linked,
        has_sync=has_sync,
        has_orders=has_orders,
        list_new_done=list_new_done,
        steps_completed=steps_completed,
        listing_errors=listing_errors,
        order_errors=order_errors,
    )


@app.route("/products")
def products():
    store_id = _current_store()
    config = load_store_config(store_id)
    published = config.get("published", {})

    try:
        data = ecwid_products(store_id, limit=100)
    except Exception as e:
        return render_template("error.html", message=str(e), store_id=store_id), 500

    items = []
    for p in data.get("items", []):
        pid = str(p.get("id"))
        # Category names
        cats = []
        for c in p.get("categories", []) or []:
            cn = c.get("name") if isinstance(c, dict) else None
            if cn:
                cats.append(cn)
        items.append({
            "id": pid,
            "name": p.get("name", ""),
            "sku": p.get("sku", ""),
            "price": p.get("price", 0),
            "quantity": p.get("quantity", 0),
            "enabled": p.get("enabled", False),
            "image": p.get("thumbnailUrl", ""),
            "categories": ", ".join(cats),
            "published": pid in published,
            "listing_id": published.get(pid, {}).get("listing_id"),
        })

    return render_template(
        "products.html",
        store_id=store_id,
        products=items,
        total=data.get("total", len(items)),
    )


@app.route("/listing_setup")
def listing_setup():
    """M2E-style: configure selected products before publishing."""
    store_id = _current_store()
    config = load_store_config(store_id)
    ebay_cfg = config.get("ebay", {})

    product_ids = request.args.getlist("product_ids")
    if not product_ids:
        return redirect(url_for("products", store_id=store_id))

    items = []
    saved_policies = config.get("product_policies", {})
    for pid in product_ids:
        try:
            p = ecwid_api(store_id, "GET", f"/products/{pid}")
            sp = saved_policies.get(pid, {})
            items.append({
                "id": str(p.get("id")),
                "name": p.get("name", ""),
                "price": p.get("price", 0),
                "quantity": p.get("quantity", 0),
                "image": p.get("thumbnailUrl", ""),
                "saved_shipping": sp.get("fulfillment_policy_id", ""),
                "saved_return": sp.get("return_policy_id", ""),
                "saved_payment": sp.get("payment_policy_id", ""),
                "saved_category": sp.get("category_id", ""),
                "saved_condition": sp.get("condition", "NEW"),
            })
        except Exception:
            continue

    return render_template(
        "listing_setup.html",
        store_id=store_id,
        products=items,
        ebay_config=ebay_cfg,
        templates=config.get("templates", {}),
    )


@app.route("/publish_configured", methods=["POST"])
def publish_configured():
    """Publish products with per-product overrides from listing setup."""
    store_id = _current_store()
    config = load_store_config(store_id)
    ebay_cfg = config.get("ebay", {})

    product_ids = request.form.getlist("product_ids")
    if not product_ids:
        return jsonify({"error": "No seleccionaste productos."}), 400

    results = []
    for pid in product_ids:
        try:
            product = ecwid_api(store_id, "GET", f"/products/{pid}")
            overrides = {
                "title": request.form.get(f"title_{pid}", product.get("name", "")),
                "price": request.form.get(f"price_{pid}", product.get("price", 0)),
                "quantity": request.form.get(f"qty_{pid}", product.get("quantity", 0)),
                "condition": request.form.get(f"condition_{pid}", "NEW"),
                "category_id": request.form.get(f"category_{pid}", ebay_cfg.get("category_id", "94845")),
                "fulfillment_policy_id": request.form.get(f"shipping_{pid}", ebay_cfg.get("fulfillment_policy_id")),
                "return_policy_id": request.form.get(f"return_{pid}", ebay_cfg.get("return_policy_id")),
                "payment_policy_id": request.form.get(f"payment_{pid}", ebay_cfg.get("payment_policy_id")),
            }
            info = ebay_publish_product(product, ebay_cfg, overrides)
            config["published"][pid] = info
            # Save per-product policy preferences
            if "product_policies" not in config:
                config["product_policies"] = {}
            config["product_policies"][pid] = {
                "fulfillment_policy_id": overrides.get("fulfillment_policy_id"),
                "return_policy_id": overrides.get("return_policy_id"),
                "payment_policy_id": overrides.get("payment_policy_id"),
                "category_id": overrides.get("category_id"),
                "condition": overrides.get("condition"),
            }
            results.append({"id": pid, "name": product.get("name", pid)[:50],
                            "ok": True, "listing_id": info["listing_id"]})
        except Exception as e:
            results.append({"id": pid, "name": pid, "ok": False, "error": str(e)[:200]})

    save_store_config(store_id, config)
    return jsonify({"results": results})


@app.route("/publish", methods=["POST"])
def publish():
    store_id = _current_store()
    config = load_store_config(store_id)
    ebay_cfg = config.get("ebay", {})

    product_ids = request.form.getlist("product_ids")
    if not product_ids:
        return jsonify({"error": "No seleccionaste productos."}), 400

    results = []
    for pid in product_ids:
        try:
            product = ecwid_api(store_id, "GET", f"/products/{pid}")
            info = ebay_publish_product(product, ebay_cfg)
            config["published"][pid] = info
            results.append({"id": pid, "ok": True, "listing_id": info["listing_id"]})
        except Exception as e:
            results.append({"id": pid, "ok": False, "error": str(e)[:200]})

    save_store_config(store_id, config)
    return jsonify({"results": results})


@app.route("/settings", methods=["GET", "POST"])
def settings():
    store_id = _current_store()
    config = load_store_config(store_id)

    if request.method == "POST":
        ebay_cfg = config.get("ebay", {})
        for key in ["payment_policy_id", "payment_policy_name",
                    "fulfillment_policy_id", "fulfillment_policy_name",
                    "return_policy_id", "return_policy_name",
                    "return_policy_30day_id", "return_policy_30day_name",
                    "merchant_location_key", "category_id"]:
            val = request.form.get(key)
            if val is not None:
                ebay_cfg[key] = val.strip()
        config["ebay"] = ebay_cfg
        save_store_config(store_id, config)
        return redirect(url_for("settings", store_id=store_id))

    return render_template(
        "settings.html",
        store_id=store_id,
        ebay=config.get("ebay", {}),
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
