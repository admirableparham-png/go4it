"""Phase 10 — the permission + role-template + data-scope catalog (the SINGLE source of truth for authorization).

Authorization is by PERMISSION + SCOPE, never by scattered role-name comparisons. A permission is a stable dotted
key (e.g. `buyer.pii.view`, `outreach.email.send`, `ai.live.use`). Role templates are editable bundles of
permissions the founder assigns; per-user overrides grant/deny individual permissions on top. `authz.py` resolves
the effective set (DEFAULT DENY) and enforces it server-side.

TWO account classes are kept strictly distinct and are NOT the same as permissions:
  * internal — Go4it staff (founder/managers/agents/researchers/…). May hold internal permissions.
  * seller   — external seller accounts. A HARD confidentiality boundary: a seller can NEVER hold any buyer-PII,
               export, outreach, cost/margin, research-source or internal permission, regardless of any role or
               override. Sellers only ever get the sanitized progress projection (enforced in authz.py).
Buyer-portal token sessions and buyer/company/contact DB records are NOT users and never enter this system.
"""

# --------------------------------------------------------------------- permission catalog
# key -> (label, group, high_risk). high_risk permissions are surfaced with a warning in the admin UI and are
# never in a template by default unless the role genuinely needs them.
_P = [
    # buyer / contact confidentiality (internal only — never a seller)
    ("buyer.pii.view",        "View buyer/contact PII",              "confidentiality", True),
    ("buyer.pii.export",      "Export buyer/contact data",           "confidentiality", True),
    # research
    ("research.view",         "View Research & Trade Network",       "research", False),
    ("research.enrich",       "Enrich Trade Network records",        "research", False),
    ("research.run",          "Run / approve Research jobs",         "research", True),
    # outreach
    ("outreach.view",         "View outreach workspace",             "outreach", False),
    ("outreach.draft",        "Draft outreach",                      "outreach", False),
    ("outreach.email.send",   "Send an individual email",            "outreach", True),
    ("outreach.campaign.manage", "Start/resume campaigns",           "outreach", True),
    ("outreach.suppression.manage", "Manage suppression",            "outreach", True),
    # products / catalog
    ("products.view",         "View products & catalog",             "products", False),
    ("products.manage",       "Manage products/catalog/pricing",     "products", False),
    ("products.publish",      "Publish seller-safe documents",       "products", True),
    # commercial
    ("commercial.view",       "View quotes/contracts/deals",         "commercial", False),
    ("quote.draft",           "Draft quotes",                        "commercial", False),
    ("quote.approve",         "Approve/send quotes",                 "commercial", True),
    ("contract.draft",        "Draft contracts",                     "commercial", False),
    ("contract.approve",      "Approve contracts",                   "commercial", True),
    ("deal.advance",          "Advance deals",                       "commercial", True),
    # operations
    ("operations.view",       "View operations",                     "operations", False),
    ("freight.manage",        "Manage freight & shipments",          "operations", True),
    ("docs.publish",          "Publish seller-safe documents",       "operations", True),
    ("delivery.manage",       "Manage delivery confirmations",       "operations", False),
    # finance / compliance
    ("finance.view",          "View payments/settlement",            "finance", False),
    ("payment.confirm",       "Confirm payments",                    "finance", True),
    ("remittance.manage",     "Manage remittance/compliance",        "finance", True),
    # intelligence / reports
    ("intelligence.view",     "View Intelligence & reports",         "intelligence", False),
    ("intelligence.report.run", "Run Intelligence reports",          "intelligence", False),
    ("intelligence.export",   "Export aggregate reports (no PII)",   "intelligence", False),
    # requests / work queue (the concierge surface)
    ("requests.view",         "View Requests & Work Queue",          "requests", False),
    ("requests.manage",       "Manage/deliver requests",             "requests", False),
    # AI / Command
    ("ai.command.use",        "Use the Command copilot (deterministic)", "ai", False),
    ("ai.live.use",           "Use LIVE Claude",                     "ai", True),
    ("ai.proposal.approve",   "Approve AI action proposals",         "ai", True),
    # administration
    ("users.manage",          "Manage users, roles & permissions",   "admin", True),
    ("audit.view",            "View security & audit records",       "admin", True),
    ("settings.manage",       "Manage platform settings",            "admin", True),
    # founder-only sentinel — the ability to grant founder-level authority / final-founder controls
    ("founder.control",       "Founder-only controls",               "founder", True),
]
PERMISSIONS = {k: {"label": lb, "group": g, "high_risk": hr} for (k, lb, g, hr) in _P}
ALL_PERMISSIONS = frozenset(PERMISSIONS)
HIGH_RISK = frozenset(k for k, m in PERMISSIONS.items() if m["high_risk"])

# permissions that are structurally forbidden to a SELLER account, no matter what a role/override says. This is
# the machine-enforced half of the seller-confidentiality rule (authz.py filters these out for sellers).
SELLER_FORBIDDEN = frozenset(
    k for k in PERMISSIONS
    if k.startswith(("buyer.", "outreach.", "research.", "commercial.", "quote.", "contract.", "deal.",
                     "finance.", "payment.", "remittance.", "intelligence.", "freight.", "docs.", "products.",
                     "ai.", "users.", "audit.", "settings.", "founder.", "requests.manage", "delivery."))
)

# --------------------------------------------------------------------- data scopes
SCOPES = ("platform", "tenant", "assigned", "own", "aggregate")
SCOPE_LABELS = {
    "platform": "Platform-wide", "tenant": "Tenant", "assigned": "Assigned records only",
    "own": "Own records only", "aggregate": "Aggregate only (no record-level PII)",
}

# --------------------------------------------------------------------- role templates (seeded; editable)
# key -> (name, account_class, default_scope, permissions). "*" = every non-founder permission (Founder also
# gets founder.control). These seed RoleTemplate rows; the founder may edit non-system templates later.
_INTERNAL_VIEW = {"research.view", "outreach.view", "products.view", "commercial.view", "operations.view",
                  "finance.view", "intelligence.view", "requests.view", "ai.command.use"}
ROLE_TEMPLATES = {
    "founder": {
        "name": "Founder", "account_class": "internal", "scope": "platform", "system": True,
        "permissions": set(ALL_PERMISSIONS),                       # everything, incl. founder.control
    },
    "admin_manager": {
        "name": "Admin / Manager", "account_class": "internal", "scope": "platform", "system": True,
        # operational management EXCLUDING founder-only controls (no founder.control; may manage users)
        "permissions": (set(ALL_PERMISSIONS) - {"founder.control", "ai.live.use"}),
    },
    "trade_agent": {
        "name": "Trade Agent", "account_class": "internal", "scope": "assigned", "system": True,
        "permissions": _INTERNAL_VIEW | {"buyer.pii.view", "requests.manage", "research.view",
                                         "outreach.draft", "quote.draft", "ai.command.use"},
    },
    "researcher": {
        "name": "Researcher", "account_class": "internal", "scope": "tenant", "system": True,
        "permissions": {"research.view", "research.enrich", "research.run", "buyer.pii.view",
                        "requests.view", "ai.command.use"},        # NO outreach send
    },
    "outreach_manager": {
        "name": "Outreach Manager", "account_class": "internal", "scope": "tenant", "system": True,
        "permissions": {"outreach.view", "outreach.draft", "outreach.email.send",
                        "outreach.campaign.manage", "outreach.suppression.manage", "buyer.pii.view",
                        "ai.command.use"},                          # NO payments
    },
    "commercial_manager": {
        "name": "Commercial Manager", "account_class": "internal", "scope": "tenant", "system": True,
        "permissions": {"commercial.view", "quote.draft", "quote.approve", "contract.draft",
                        "contract.approve", "deal.advance", "products.view", "buyer.pii.view",
                        "ai.command.use"},
    },
    "operations_manager": {
        "name": "Operations Manager", "account_class": "internal", "scope": "tenant", "system": True,
        "permissions": {"operations.view", "freight.manage", "docs.publish", "delivery.manage",
                        "products.publish", "requests.view", "ai.command.use"},
    },
    "finance_compliance": {
        "name": "Finance / Compliance", "account_class": "internal", "scope": "tenant", "system": True,
        "permissions": {"finance.view", "payment.confirm", "remittance.manage", "commercial.view",
                        "audit.view", "ai.command.use"},            # NO buyer.pii.export by default
    },
    "analyst": {
        "name": "Analyst", "account_class": "internal", "scope": "aggregate", "system": True,
        # intelligence + aggregate exports, but explicitly NO buyer/contact PII
        "permissions": {"intelligence.view", "intelligence.report.run", "intelligence.export",
                        "ai.command.use"},
    },
    "auditor": {
        "name": "Read-only Auditor", "account_class": "internal", "scope": "platform", "system": True,
        "permissions": {"audit.view"} | _INTERNAL_VIEW,             # read-only: views + audit, no mutations
    },
    "seller": {
        "name": "Seller", "account_class": "seller", "scope": "own", "system": True,
        "permissions": set(),        # sellers get the sanitized seller dashboard ONLY — no internal permissions
    },
}
# the role a NEW account of each class defaults to, and the templates a non-founder admin may assign (never
# founder, never a template carrying founder.control).
DEFAULT_ROLE = {"internal": "trade_agent", "seller": "seller"}
FOUNDER_ROLE = "founder"


def template_permissions(role_key: str) -> set:
    t = ROLE_TEMPLATES.get(role_key)
    return set(t["permissions"]) if t else set()


def is_seller_role(role_key: str) -> bool:
    return ROLE_TEMPLATES.get(role_key, {}).get("account_class") == "seller"
