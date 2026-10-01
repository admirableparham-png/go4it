"""Phase 11 — the ONE place a campaign email is rendered. The campaign send path (campaign_service.send_step) AND
the zero-send dry-run (scripts/campaign_dryrun.py) both call render_campaign_message, so what the dry-run checks is
exactly what goes out.

Fail-closed rules — a message that breaks one is never sent:
  * merge fields are an allow-list ({company} {country} {city}); an unknown or unfilled placeholder, a stray brace
    or another tool's merge tag ({{x}}, *|X|*, %%x%%) never reaches a buyer;
  * every message carries a mailto List-Unsubscribe header (and, when configured on the mailbox, a footer with
    company + postal address + opt-out line in BOTH parts) — the easiest opt-out that stays on the sender's own domain;
  * the seller's identity never reaches a buyer (two-way confidentiality) — a hit BLOCKS the send, it is never
    sent as "[redacted]";
  * the internal platform brand (g4it / go4it) never appears in anything a buyer sees.
The admin-authored HTML design is sanitized (no scripts, handlers, non-http(s) URLs); email clients don't run
scripts anyway — this is defence in depth for the in-app preview.
"""
import html as _html
import re
from html.parser import HTMLParser

from . import send_guard as SG
from .countries import country_name

MERGE_FIELDS = ("company", "country", "city")
OPT_OUT_LINE = 'Not relevant? Reply "unsubscribe" and we will not email you again.'
MAX_HTML_BYTES = 90_000          # Gmail clips at ~102 KB — a clipped message would hide the footer
MAX_SUBJECT = 200

_PLACEHOLDER = re.compile(r"\{\s*([A-Za-z_]\w*)\s*\}")
_FOREIGN_TAG = re.compile(r"\{\{.*?\}\}|\*\|.*?\|\*|%%[^%\s]+%%|\[\[.*?\]\]", re.S)
INTERNAL_BRAND = re.compile(r"(?<![a-z0-9])go?-?4-?it(?![a-z0-9])", re.I)


# --------------------------------------------------------------------- template + sender validation
def validate_step(subject, body, body_html="") -> list:
    """Template-level problems (the same for every recipient). Empty list = the step can be saved / sent."""
    errs = []
    if not (subject or "").strip():
        errs.append("a subject is required")
    if not (body or "").strip():
        errs.append("a text body is required (every email carries a plain-text part)")
    for label, part in (("subject", subject), ("text", body), ("HTML", body_html)):
        if not part:
            continue
        if _FOREIGN_TAG.search(part):
            errs.append(f"{label}: merge tags like {{{{x}}}}, *|X|* or %%x%% are not supported — "
                        "use {company}, {country} or {city}")
        unknown = sorted({m.group(1) for m in _PLACEHOLDER.finditer(part)
                          if m.group(1).lower() not in MERGE_FIELDS})
        if unknown:
            errs.append(f"{label}: unknown merge field(s) " + ", ".join("{%s}" % u for u in unknown))
        if label != "HTML":                     # CSS braces are legitimate inside the HTML design
            rest = _PLACEHOLDER.sub("", part)
            if "{" in rest or "}" in rest:
                errs.append(f"{label}: stray {{ or }} — only {{company}}, {{country}} and {{city}} are allowed")
        if INTERNAL_BRAND.search(part):
            errs.append(f"{label}: mentions the internal platform name — buyers must only see the company brand")
    if body_html and len(sanitize_html(body_html).encode("utf-8")) > MAX_HTML_BYTES:
        errs.append("the HTML design is over 90 KB — Gmail would clip it and hide the footer")
    return errs


def validate_sender(mailbox) -> list:
    """The footer every buyer email must carry comes from the sending mailbox."""
    if mailbox is None:
        return ["no sending mailbox"]
    errs = []
    # the visible footer (company + postal address + opt-out line) is OPTIONAL — founder's choice (2026-10-01): the
    # email's own sign-off is the signature; the List-Unsubscribe header is always sent. If one footer field is set,
    # both must be, so a footer is never half-filled.
    has_co = bool((getattr(mailbox, "sender_company", "") or "").strip())
    has_addr = bool((getattr(mailbox, "postal_address", "") or "").strip())
    if has_co != has_addr:
        errs.append("the footer needs both the sender company and the postal address (or neither)")
    for label, v in (("from name", mailbox.from_name), ("sender company", getattr(mailbox, "sender_company", "")),
                     ("postal address", getattr(mailbox, "postal_address", "")), ("mailbox address", mailbox.email)):
        if v and INTERNAL_BRAND.search(v):
            errs.append(f"the {label} mentions the internal platform name")
    return errs


# --------------------------------------------------------------------- merge fields
def _clean(value, limit):
    return re.sub(r"\s+", " ", (value or "").replace("{", "").replace("}", "")).strip()[:limit]


# trailing legal forms a person never says in a greeting ("Hi Inoxa team", not "Hi Inoxa Sp. z o.o. team")
_LEGAL_TAIL = re.compile(
    r"(?:[\s,]+(?:ltd\.?|limited|llc|l\.l\.c\.?|inc\.?|incorporated|corp\.?|corporation|co\.|company limited|"
    r"gmbh(?:\s*&\s*co\.?\s*kg)?|kg|ag|ab|a/s|as|oy|oyj|plc|pty\.?(?:\s*ltd\.?)?|b\.?v\.?|n\.?v\.?|s\.?a\.?s?|"
    r"s\.?a\.?r\.?l\.?|s\.?p\.?a\.?|s\.?r\.?l\.?|s\.?l\.?|s\.?l\.?u\.?|ltda\.?|sp\.?\s*z\s*o\.?\s*o\.?|sp\.?\s*j\.?|"
    r"s\.?r\.?o\.?|a\.?s\.?|kft\.?|d\.?o\.?o\.?|fze|fzco|fz-llc|fzc|w\.?l\.?l\.?))+\s*$", re.I)


def display_company(name) -> str:
    """The company name as a person would write it in 'Hi … team': bracketed notes and trailing legal forms removed
    ("IHL Canada (Investments Hardware Ltd.)" → "IHL Canada", "D.M.P. STEEL, s.r.o." → "D.M.P. STEEL"); the brand
    itself is never re-cased or shortened. Falls back to the cleaned original if nothing sensible would remain."""
    raw = _clean(name, 200)
    n = re.sub(r"\s*\([^()]*\)", "", raw).strip()          # "(formerly …)", "(Gruppo …)", "(city, notes)"
    n = _LEGAL_TAIL.sub("", n).strip(" ,-–—/|")
    return (n if len(n) >= 2 else raw)[:80]


def merge_values(lead) -> dict:
    city = _clean(getattr(lead, "dest_city", ""), 200)
    if "(" in city or len(city) > 24:           # addresses / notes stored as "city" never read as a city name
        city = ""
    return {"company": display_company(getattr(lead, "buyer_company", "")),
            "country": country_name(getattr(lead, "dest_country", "")),
            "city": city}


def _merge(template, values, escape=False):
    missing = set()

    def sub(m):
        key = m.group(1).lower()
        val = values.get(key, "")
        if not val:
            missing.add(key)
            return m.group(0)
        return _html.escape(val) if escape else val
    return _PLACEHOLDER.sub(sub, template or ""), missing


# --------------------------------------------------------------------- footer + headers
def footer_text(mailbox) -> str:
    lines = ["-- ", mailbox.sender_company.strip()]
    lines += [ln.strip() for ln in mailbox.postal_address.splitlines() if ln.strip()]
    lines += ["", OPT_OUT_LINE]
    return "\n".join(lines)


def footer_html(mailbox, centered=False) -> str:
    """centered=True for a designed email (the footer sits under the card); plain emails keep it left-aligned."""
    esc = _html.escape
    addr = "<br>".join(esc(ln.strip()) for ln in mailbox.postal_address.splitlines() if ln.strip())
    mailto = f"mailto:{esc(mailbox.email.strip())}?subject=unsubscribe"
    box = ("max-width:600px;margin:0 auto 24px;padding:14px 28px 0;box-sizing:border-box;" if centered
           else "max-width:620px;margin:24px 0 0;padding-top:12px;")
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.5;color:#6b7280;'
            f'{box}border-top:1px solid #e5e7eb;">'
            f'<div style="font-weight:bold;color:#374151;">{esc(mailbox.sender_company.strip())}</div>'
            f'<div>{addr}</div>'
            f'<div style="margin-top:8px;">{esc(OPT_OUT_LINE)} '
            f'<a href="{mailto}" style="color:#6b7280;text-decoration:underline;">Stop receiving these emails</a>'
            '</div></div>')


def has_footer(mailbox) -> bool:
    return bool((getattr(mailbox, "sender_company", "") or "").strip()
                and (getattr(mailbox, "postal_address", "") or "").strip())


# --------------------------------------------------------------------- template attachment (founder's own file)
MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024
_REPO = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))


def load_attachment(rel_path):
    """(filename, bytes, error) for a campaign template's attachment. ONLY a PDF that ships with the code under
    campaigns/ (the founder's own price list) — never a user upload, which stays blocked (app/attachments.py)."""
    import os
    if not rel_path:
        return "", b"", ""
    base = os.path.realpath(os.path.join(_REPO, "campaigns"))
    full = os.path.realpath(os.path.join(_REPO, rel_path))
    if not full.startswith(base + os.sep) or not full.lower().endswith(".pdf"):
        return "", b"", "the attachment must be a PDF inside the campaigns/ folder"
    if not os.path.isfile(full):
        return "", b"", f"the attachment {rel_path} is missing on this server"
    data = open(full, "rb").read()
    if not data.startswith(b"%PDF"):
        return "", b"", "the attachment is not a real PDF"
    if len(data) > MAX_ATTACHMENT_BYTES:
        return "", b"", "the attachment is over 3 MB"
    return os.path.basename(full), data, ""


def list_unsubscribe(mailbox) -> str:
    return f"<mailto:{mailbox.email.strip()}?subject=unsubscribe>"


def _with_footer(html_doc, foot):
    i = html_doc.lower().rfind("</body>")
    return html_doc[:i] + foot + html_doc[i:] if i >= 0 else html_doc + foot


# --------------------------------------------------------------------- seller-identity guard (detection, fail closed)
def _seller_needles(session, tenant_id) -> list:
    """Everything that identifies the seller: account name/email, connected mailboxes, profile company/names."""
    if not tenant_id:
        return []
    from sqlmodel import select
    from .models import MailAccount, User, UserProfile
    seller = session.get(User, tenant_id)
    if seller is None:
        return []
    prof = session.exec(select(UserProfile).where(UserProfile.user_id == tenant_id)).first()
    raw = [seller.email, seller.name] + [m.email for m in session.exec(
        select(MailAccount).where(MailAccount.user_id == tenant_id)).all()]
    if prof:
        raw += [prof.company, prof.full_name, prof.display_name]
    out = []
    for n in raw:
        n = " ".join((n or "").split()).lower()
        if len(n) > 2 and n not in out:
            out.append(n)
    return out


def _names_seller(needles, *parts) -> bool:
    """Whitespace-normalised, case-insensitive. Names of 5+ characters match anywhere (so 'sharklinetrading.com' or
    'SHARKLINE-TR' are caught); shorter ones only as whole words (so 'Ali' never blocks 'quality')."""
    hay = " ".join(" ".join((p or "").split()) for p in parts).lower()
    for n in needles:
        if "@" in n or len(n) >= 5:
            if n in hay:
                return True
        elif re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", hay):
            return True
    return False


def _visible(html_doc) -> str:
    """What a buyer can read in an HTML part: its text (entities decoded, tags removed) + link targets / alt text."""
    attrs = " ".join(_html.unescape(v) for v in re.findall(r'(?:href|src|alt|title)="([^"]*)"', html_doc or ""))
    return html_to_text(html_doc) + " " + attrs


def template_problems(session, campaign, step, mailbox) -> list:
    """Everything wrong with a step + its sending mailbox that would be wrong for EVERY buyer (fix once)."""
    subject_t, body_t = step.subject or "", step.body or ""
    html_t = getattr(step, "body_html", "") or ""
    errs = validate_step(subject_t, body_t, html_t) + validate_sender(mailbox)
    _n, _d, att_err = load_attachment(getattr(step, "attachment_path", "") or "")
    if att_err:
        errs.append(att_err)
    needles = _seller_needles(session, campaign.tenant_id)
    if needles:
        for label, part in (("subject", subject_t), ("text", body_t),
                            ("HTML", _visible(sanitize_html(html_t)) if html_t else "")):
            if part and _names_seller(needles, part):
                errs.append(f"the {label} names the seller — buyers must never learn who the seller is")
        if mailbox is not None and _names_seller(needles, mailbox.from_name, getattr(mailbox, "sender_company", ""),
                                                 getattr(mailbox, "postal_address", "")):
            errs.append("the mailbox's from name / footer names the seller")
    return errs


# --------------------------------------------------------------------- the render
def render_campaign_message(session, campaign, step, lead, mailbox) -> dict:
    """{ok, scope, error, subject, text, html, headers}. scope='template' = the step/mailbox itself is wrong (fix it
    once, the campaign should pause); scope='recipient' = only this buyer can't be rendered (skip them). Reads only."""
    def fail(scope, error):
        return {"ok": False, "scope": scope, "error": error[:400], "subject": "", "text": "", "html": "",
                "headers": {}, "attachments": []}
    errs = template_problems(session, campaign, step, mailbox)
    if errs:
        return fail("template", "; ".join(errs))
    subject_t, body_t = step.subject or "", step.body or ""
    html_t = getattr(step, "body_html", "") or ""
    if lead is None:
        return fail("recipient", "no buyer record")
    values = merge_values(lead)
    subject, m1 = _merge(subject_t, values)
    text, m2 = _merge(body_t, values)
    html_body, m3 = _merge(html_t, values, escape=True) if html_t else ("", set())
    missing = m1 | m2 | m3
    if missing:
        return fail("recipient", "no value for " + ", ".join("{%s}" % k for k in sorted(missing)))
    subject = SG.sanitize_header(subject)
    if len(subject) > MAX_SUBJECT:
        return fail("recipient", "the subject is over 200 characters after merging")
    html_seen = _visible(sanitize_html(html_body)) if html_body else ""
    if _names_seller(_seller_needles(session, campaign.tenant_id), subject, text, html_seen):
        return fail("recipient", "the email names the seller after merging this buyer's details")
    for label, part in (("subject", subject), ("text", text), ("HTML", html_body + " " + html_seen)):
        if part.strip() and INTERNAL_BRAND.search(part):
            return fail("recipient", f"the {label} mentions the internal platform name after merging")
    from .outreach import plain_parts
    base = sanitize_html(html_body) if html_body else plain_parts(text)[1]
    if has_footer(mailbox):
        text_full = text.rstrip() + "\n\n" + footer_text(mailbox)
        html_full = _with_footer(base, footer_html(mailbox, centered=bool(html_body)))
    else:
        text_full, html_full = text.rstrip() + "\n", base
    if len(html_full.encode("utf-8")) > MAX_HTML_BYTES:
        return fail("template", "the HTML is over 90 KB — Gmail would clip it")
    name, data, _err = load_attachment(getattr(step, "attachment_path", "") or "")
    return {"ok": True, "scope": "", "error": "", "subject": subject, "text": text_full, "html": html_full,
            "headers": {"List-Unsubscribe": list_unsubscribe(mailbox)},
            "attachments": [(name, data)] if name else []}


# --------------------------------------------------------------------- HTML sanitizer (stdlib only)
_DROP_WITH_CONTENT = {"script", "iframe", "object", "svg", "math", "noscript", "template", "select", "textarea",
                      "button", "applet", "frameset", "frame", "audio", "video", "canvas"}
_DROP_TAG_ONLY = {"embed", "input", "base", "link", "form", "param", "source", "track", "portal", "option"}
_DROP_ATTRS = {"srcdoc", "action", "formaction", "xlink:href", "data", "ping"}
_URL_ATTRS = {"href", "src", "background", "poster", "srcset", "lowsrc", "dynsrc", "longdesc", "cite"}
_BAD_CSS = re.compile(r"expression\s*\(|javascript:|vbscript:|behavior\s*:|-moz-binding|@import|"
                      r"url\s*\(\s*['\"]?\s*(?:data|javascript|vbscript):", re.I)


def _url_ok(attr, value):
    v = re.sub(r"[\x00-\x20]", "", _html.unescape(value or "")).lower()   # defeats "java\tscript:" / entities
    if attr == "href":
        return v.startswith(("http://", "https://", "mailto:", "tel:", "#"))
    if attr == "srcset":
        return all(p.strip().startswith(("http://", "https://")) for p in v.split(",") if p.strip())
    return v.startswith(("http://", "https://"))


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out = []
        self._skip = []          # stack of dropped container tags (their whole content is dropped)
        self._style = None       # buffer while inside <style>

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, closed=True)

    def _start(self, tag, attrs, closed):
        if self._skip:
            if tag == self._skip[-1] and not closed:
                self._skip.append(tag)
            return
        if tag in _DROP_WITH_CONTENT:
            if not closed:
                self._skip.append(tag)
            return
        if tag in _DROP_TAG_ONLY or (tag == "meta" and any((k or "").lower() == "http-equiv" for k, _ in attrs)):
            return
        if tag == "style":
            if not closed:
                self._style = []
            return
        clean = []
        for k, v in attrs:
            k = (k or "").lower()
            if k.startswith("on") or k in _DROP_ATTRS:
                continue
            if k in _URL_ATTRS and v is not None and not _url_ok(k, v):
                continue
            if k == "style" and v and _BAD_CSS.search(_html.unescape(v)):
                continue
            clean.append(k if v is None else f'{k}="{_html.escape(v, quote=True)}"')
        self.out.append("<" + " ".join([tag] + clean) + (" />" if closed else ">"))

    def handle_endtag(self, tag):
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.pop()
            return
        if tag == "style":
            if self._style is not None:
                css = "".join(self._style)
                if not _BAD_CSS.search(css):
                    self.out.append(f"<style>{css}</style>")
                self._style = None
            return
        if tag in _DROP_WITH_CONTENT or tag in _DROP_TAG_ONLY:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._skip:
            return
        if self._style is not None:
            self._style.append(data)
            return
        self.out.append(_html.escape(data, quote=False))

    def handle_entityref(self, name):
        self._ref(f"&{name};")

    def handle_charref(self, name):
        self._ref(f"&#{name};")

    def _ref(self, ref):
        if self._skip:
            return
        (self._style if self._style is not None else self.out).append(ref)

    def handle_comment(self, data):
        if self._skip or self._style is not None:
            return
        d = data.strip()
        mso = d.startswith("[if") or d.startswith("<![endif]") or d == "[endif]" or d.endswith("<![endif]")
        if mso and not re.search(r"<\s*script|javascript:", d, re.I):
            self.out.append(f"<!--{data}-->")      # Outlook conditional comments only

    def handle_decl(self, decl):
        if decl.lower().startswith("doctype"):
            self.out.append(f"<!{decl}>")

    def unknown_decl(self, data):
        pass

    def handle_pi(self, data):
        pass


def sanitize_html(doc) -> str:
    p = _Sanitizer()
    p.feed(doc or "")
    p.close()
    return "".join(p.out)


# --------------------------------------------------------------------- HTML → text (replies, derived text parts)
_BLOCK = {"p", "div", "br", "li", "tr", "table", "ul", "ol", "hr", "blockquote", "h1", "h2", "h3", "h4", "h5",
          "h6", "section", "article", "header", "footer"}
_VOID = {"br", "img", "hr", "meta", "input", "link", "area", "base", "col", "embed", "source", "track", "wbr"}


class _Text(HTMLParser):
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self, drop_quotes):
        super().__init__(convert_charrefs=True)
        self.parts, self._skip, self._quote, self.drop_quotes = [], 0, 0, drop_quotes

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
            return
        if self._quote:
            if tag not in _VOID:
                self._quote += 1
            return
        if self.drop_quotes and tag not in _VOID:
            a = {k: (v or "") for k, v in attrs}
            cls, idv = a.get("class", "").lower(), a.get("id", "").lower()
            if (tag == "blockquote" or "gmail_quote" in cls or "yahoo_quoted" in cls or "moz-cite-prefix" in cls
                    or idv in ("divrplyfwdmsg", "appendonsend")):
                self._quote = 1          # quoted history: everything inside is dropped
                return
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._quote:
            self._quote -= 1
            return
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip and not self._quote:
            self.parts.append(data)


def html_to_text(doc, drop_quotes=False) -> str:
    p = _Text(drop_quotes)
    p.feed(doc or "")
    p.close()
    lines = [re.sub(r"[ \t\r\f\v ]+", " ", ln).strip() for ln in "".join(p.parts).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
