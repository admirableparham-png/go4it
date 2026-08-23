"""Central admin navigation configuration — the SINGLE source for both header levels.

This is *display-only*. Labels here never rename a backend model, route, status value or source id: the
backend keeps using `Lead` while the header reads "Buyers & Prospects". Every destination is an EXISTING
named route (resolved through `request.url_for`, never a hard-coded URL) so links can't silently drift, and
backend authorization stays the source of truth — this module only decides which links to *render*; the
route handlers still enforce who may open them.

Structure: a primary workspace is either `direct` (Dashboard / Requests — opens its page immediately) or has
`children` shown in the contextual secondary row. Active state is derived from the request PATH (via each
item's `paths` prefixes) so list, detail, edit and action pages all light up the right parent + child
without every route having to pass an `active` flag.
"""

# Ordered primary workspaces. `paths` are path prefixes that mark an item active (prefix match on a path
# segment, so /leads and /leads/42 match but /leadsX does not). `endpoint` is the route function name.
WORKSPACES = [
    {"key": "dashboard", "label": "Dashboard", "icon": "home", "role": "admin",
     "direct": {"endpoint": "dashboard", "paths": ("/",)}},

    {"key": "work_queue", "label": "Work Queue", "icon": "check-square", "role": "admin",
     "badge_endpoint": "work_queue_count",
     "direct": {"endpoint": "work_queue", "paths": ("/admin/work-queue",)}},

    {"key": "requests", "label": "Requests", "icon": "inbox", "role": "admin",
     "badge_endpoint": "admin_requests_count",
     "direct": {"endpoint": "admin_requests", "paths": ("/admin/requests",)}},

    {"key": "network", "label": "Network", "icon": "users", "role": "admin", "children": [
        {"key": "leads", "label": "Buyers & Prospects", "endpoint": "leads_list",
         "paths": ("/leads", "/companies")},   # company detail lights up Buyers & Prospects
        {"key": "sellers", "label": "Sellers", "endpoint": "sellers_list", "paths": ("/sellers",)},
        {"key": "suppliers", "label": "Suppliers", "endpoint": "suppliers_list", "paths": ("/suppliers",)},
        {"key": "dataquality", "label": "Data Quality", "endpoint": "data_quality", "paths": ("/data-quality",)},
        {"key": "duplicates", "label": "Duplicate Review", "endpoint": "duplicates_list", "paths": ("/duplicates",)},
    ]},

    {"key": "outreach", "label": "Outreach", "icon": "send", "role": "admin", "children": [
        {"key": "campaign", "label": "Campaigns", "endpoint": "campaign_dashboard", "paths": ("/campaign",)},
        {"key": "mail", "label": "Email Accounts", "endpoint": "mail_accounts", "paths": ("/mail",)},
    ]},

    {"key": "products", "label": "Products", "icon": "box", "role": "admin", "children": [
        {"key": "catalog", "label": "Catalog", "endpoint": "catalog", "paths": ("/catalog",)},
        {"key": "rates", "label": "Rates & Costs", "endpoint": "rates_page", "paths": ("/rates",)},
    ]},

    {"key": "commercial", "label": "Commercial", "icon": "briefcase", "role": "admin", "children": [
        {"key": "quotes", "label": "Quotes", "endpoint": "quotes_list", "paths": ("/quotes",)},
        {"key": "deals", "label": "Deals", "endpoint": "deals_list", "paths": ("/deals",)},
    ]},

    {"key": "intelligence", "label": "Intelligence", "icon": "compass", "role": "admin", "children": [
        {"key": "research", "label": "Research", "endpoint": "research", "paths": ("/research",)},
        {"key": "intel", "label": "Market Intel", "endpoint": "intel", "paths": ("/intel",)},
        {"key": "markets", "label": "Markets", "endpoint": "markets", "paths": ("/markets", "/georgia", "/uae")},
        {"key": "lines", "label": "Product Lines", "endpoint": "lines_hub", "params": {"slug": "cd-dvd"},
         "paths": ("/lines",)},
        {"key": "command", "label": "Command", "endpoint": "command_page", "paths": ("/command",)},
    ]},

    {"key": "admin", "label": "Admin", "icon": "shield", "role": "admin", "children": [
        {"key": "ingest", "label": "Data Imports", "endpoint": "ingest_page", "paths": ("/ingest",)},
        {"key": "users", "label": "Accounts & Access", "endpoint": "admin_users", "paths": ("/admin/users",)},
        {"key": "activity", "label": "Team Activity", "endpoint": "admin_activity", "paths": ("/admin/activity",)},
    ]},
]

# Read-only registry of existing market (country) pages — the Markets landing page ONLY organizes access to
# these; it never combines, moves or rewrites the underlying datasets. Add a country here to surface it.
MARKETS = [
    {"key": "georgia", "name": "Georgia", "iso": "GE", "endpoint": "georgia",
     "summary": "Live Georgian tenders, gym / venue demand signals, and customs market reality."},
    {"key": "uae", "name": "United Arab Emirates", "iso": "AE", "endpoint": "uae",
     "summary": "UAE supply-side sourcing and the CD/DVD product-line intel."},
]


def _norm(path):
    return path if path == "/" else (path or "/").rstrip("/")


def _match(paths, cur):
    """True if the current path belongs to this item (exact, or a child path under it)."""
    for p in paths:
        if p == "/":
            if cur == "/":
                return True
        elif cur == p or cur.startswith(p + "/"):
            return True
    return False


def _can(user, role):
    """Render-time visibility. All admin nav requires the admin role; this mirrors (never replaces) the
    backend gate on each route."""
    if role == "admin":
        return getattr(user, "role", None) == "admin"
    return True


def build_nav(request, user):
    """Return the admin header model, or None for non-admins (they keep their own unchanged nav).

    {
      "primary":   [ {key,label,icon,href,active,direct,badge_endpoint,children:[{key,label,href,active}]} ],
      "secondary": [ {key,label,href,active} ]  # children of the active workspace (empty if direct/none),
      "active_workspace": key|None,
      "active_label": str,   # current page label for the mobile header title
    }
    """
    if not user or getattr(user, "role", None) != "admin":
        return None
    cur = _norm(request.url.path)

    def path_for(endpoint, params):
        # named-route resolution -> relative path (no hard-coded URLs; NoMatchFound surfaces bad configs)
        return request.url_for(endpoint, **(params or {})).path

    primary, secondary, active_ws, active_label = [], [], None, ""
    for ws in WORKSPACES:
        if not _can(user, ws.get("role", "admin")):
            continue
        if ws.get("direct"):
            d = ws["direct"]
            is_active = _match(d.get("paths", ()), cur)
            badge_ep = ws.get("badge_endpoint", "")
            primary.append({
                "key": ws["key"], "label": ws["label"], "icon": ws.get("icon", ""),
                "href": path_for(d["endpoint"], d.get("params")), "active": is_active, "direct": True,
                "badge_href": path_for(badge_ep, None) if badge_ep else "", "children": [],
            })
            if is_active:
                active_ws, active_label = ws["key"], ws["label"]
            continue

        kids = []
        ws_active = False
        for k in ws["children"]:
            if not _can(user, k.get("role", "admin")):
                continue
            ka = _match(k.get("paths", ()), cur)
            if ka:
                ws_active = True
                active_label = k["label"]
            kids.append({"key": k["key"], "label": k["label"],
                         "href": path_for(k["endpoint"], k.get("params")), "active": ka})
        if not kids:
            continue
        primary.append({
            "key": ws["key"], "label": ws["label"], "icon": ws.get("icon", ""),
            "href": kids[0]["href"], "active": ws_active, "direct": False,
            "badge_href": "", "children": kids,
        })
        if ws_active:
            active_ws, secondary = ws["key"], kids

    return {"primary": primary, "secondary": secondary,
            "active_workspace": active_ws, "active_label": active_label}
