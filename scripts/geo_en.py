"""Georgian -> English helper for the harvesters.

Two jobs so nothing Georgian-script reaches the shareable files or the CRM:
  * translit_ka(s) - transliterate Georgian (Mkhedruli) letters to Latin.
  * geo_to_en(s)   - translate common org/administrative words to English, then
                     transliterate whatever Georgian remains (place names etc.).
  * cpv_en(code)   - official English label for the CPV codes we filter on.

Not a full translator - it yields readable English proper-noun + role names
(e.g. "kaspis municipality", "batumi sports center"), which is what we need.
"""
import re
import unicodedata

# Mkhedruli letter -> Latin (national-style, brand-friendly f for phi).
_KA = {
    "ა": "a", "ბ": "b", "გ": "g", "დ": "d", "ე": "e", "ვ": "v", "ზ": "z", "თ": "t",
    "ი": "i", "კ": "k", "ლ": "l", "მ": "m", "ნ": "n", "ო": "o", "პ": "p", "ჟ": "zh",
    "რ": "r", "ს": "s", "ტ": "t", "უ": "u", "ფ": "f", "ქ": "k", "ღ": "gh", "ყ": "q",
    "შ": "sh", "ჩ": "ch", "ც": "ts", "ძ": "dz", "წ": "ts", "ჭ": "ch", "ხ": "kh",
    "ჯ": "j", "ჰ": "h", "ჳ": "w", "ჱ": "e", "ჲ": "y", "ჴ": "q", "ჵ": "o", "ჶ": "f",
}
_KA_RANGE = r"Ⴀ-ჿ"

# Word/stem -> English, longest stem first; trailing Georgian case-endings are consumed.
_DICT = [
    ("სკოლამდელი", "Preschool"), ("მუნიციპალიტეტ", "Municipality"),
    ("უნივერსიტეტ", "University"), ("დაწესებულებ", "Institution"),
    ("განვითარებ", "Development"), ("სახელოვნებო", "Arts"), ("ხელშეწყობ", "Support"),
    ("გაერთიანებ", "Association"), ("სახელმწიფო", "State"), ("განათლებ", "Education"),
    ("სააგენტო", "Agency"), ("სპორტულ", "Sports"), ("ანსამბლ", "Ensemble"),
    ("ტურიზმ", "Tourism"), ("კულტურ", "Culture"), ("სიმღერ", "Song"),
    ("ბიბლიოთეკ", "Library"), ("საავადმყოფ", "Hospital"), ("სპორტ", "Sport"),
    ("ცეკვ", "Dance"), ("მერიი", "City Hall"), ("მერია", "City Hall"),
    ("ცენტრ", "Center"), ("სკოლ", "School"), ("აღზრდ", "Upbringing"),
    ("ბაღ", "Kindergarten"), ("საჯარო", "Public"), ("რესურს", "Resource"),
    ("ახალგაზრდ", "Youth"), ("კომპლექს", "Complex"), ("დარბაზ", "Hall"),
    # tender statuses / procurement terms
    ("გამოცხადებულ", "Announced"), ("ელექტრონული", "Electronic"), ("შესყიდვ", "Procurement"),
    ("მიმდინარე", "Ongoing"), ("დასრულებ", "Completed"), ("დასრულდ", "Completed"),
    ("შეფასებ", "Under evaluation"), ("შერჩევ", "Selection"), ("ხელშეკრულებ", "Contract"),
    ("გამარტივებული", "Simplified"), ("კონსოლიდირებ", "Consolidated"),
]
_DICT.sort(key=lambda kv: -len(kv[0]))


def translit_ka(s):
    if not s:
        return s
    out = []
    for ch in s:
        cp = ord(ch)
        if 0x10A0 <= cp <= 0x10C5:      # Asomtavruli (caps) -> Mkhedruli
            ch = chr(cp + 0x30)
            cp = ord(ch)
        if ch in _KA:
            out.append(_KA[ch])
        elif 0x10A0 <= cp <= 0x10FF:    # unmapped Georgian glyph -> drop
            continue
        else:
            out.append(ch)
    return "".join(out)


def _title(s):
    s = re.sub(r"\s+", " ", s).strip()
    return " ".join(w[:1].upper() + w[1:] if (w and w[0].islower()) else w
                    for w in s.split(" "))


def geo_to_en(s):
    if not s:
        return s
    if not re.search(f"[{_KA_RANGE}]", s):   # already Latin
        return s
    s = s.replace("ა(ა)იპ", "NNLE ").replace("შპს", "LLC ")
    s = re.sub(r"(?<=\s)და(?=\s)", "and", s)
    for stem, en in _DICT:
        s = re.sub(stem + f"[{_KA_RANGE}]*", en, s)
    s = translit_ka(s)
    return _title(s)


_CPV = [
    ("45112720", "Landscaping for sports grounds"), ("44112200", "Floor coverings"),
    ("37535", "Playground equipment"), ("37450", "Sports field equipment"),
    ("37415", "Sports equipment"), ("37410", "Outdoor sports equipment"),
    ("37400", "Sports goods and equipment"), ("45236", "Surfacing / flatwork"),
    ("44113", "Road-surfacing materials"), ("45233", "Road construction / repair"),
    ("19511", "Rubber products"), ("19510", "Rubber products"),
]


def cpv_en(code):
    code = (code or "").strip()
    for prefix, label in _CPV:
        if code.startswith(prefix):
            return label
    return ""


# --- Persian / Arabic-script transliteration (for the Iran supplier directory) ------
_FA = {
    "ا": "a", "آ": "a", "أ": "a", "إ": "e", "ب": "b", "پ": "p", "ت": "t", "ث": "s",
    "ج": "j", "چ": "ch", "ح": "h", "خ": "kh", "د": "d", "ذ": "z", "ر": "r", "ز": "z",
    "ژ": "zh", "س": "s", "ش": "sh", "ص": "s", "ض": "z", "ط": "t", "ظ": "z", "ع": "'",
    "غ": "gh", "ف": "f", "ق": "q", "ک": "k", "ك": "k", "گ": "g", "ل": "l", "م": "m",
    "ن": "n", "و": "v", "ه": "h", "ة": "h", "ی": "i", "ي": "y", "ئ": "y", "ء": "'",
    "ؤ": "u", "‌": " ", "‏": "", "‎": "",
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
}
_FA_CITY = {
    "تهران": "Tehran", "اصفهان": "Isfahan", "شیراز": "Shiraz", "قزوین": "Qazvin",
    "مشهد": "Mashhad", "تبریز": "Tabriz", "کرج": "Karaj", "یزد": "Yazd",
    "اهواز": "Ahvaz", "قم": "Qom", "رشت": "Rasht", "کرمان": "Kerman",
    "ساوه": "Saveh", "البرز": "Alborz", "گیلان": "Gilan", "مازندران": "Mazandaran",
}


def fa_translit(s):
    if not s:
        return s
    s = _FA_CITY.get(s.strip(), None) or s
    if not re.search(r"[؀-ۿﭐ-ﻼ]", s):
        return s
    out = []
    for ch in s:
        cp = ord(ch)
        if ch in _FA:
            out.append(_FA[ch])
        elif (0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F
              or 0xFB50 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFF or cp == 0x200C):
            continue                    # unmapped Arabic-script mark/punct -> drop
        else:
            out.append(ch)
    return _title(re.sub(r"\s+", " ", "".join(out)).strip())


# --- Cyrillic transliteration (Kazakhstan/Azerbaijan/CIS OSM names) ------------------
# Russian base + the extra letters used in Kazakh/Azerbaijani/Ukrainian Cyrillic.
_RU = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
    # Kazakh / Azerbaijani / Ukrainian extras
    "ә": "a", "ғ": "gh", "қ": "q", "ң": "ng", "ө": "o", "ұ": "u", "ү": "u", "һ": "h",
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ђ": "dj", "ј": "j", "љ": "lj", "њ": "nj",
}


def ru_translit(s):
    if not s:
        return s
    if not re.search(r"[А-Яа-яЀ-ӿ]", s):
        return s
    out = []
    for ch in s:
        low = ch.lower()
        if low in _RU:
            t = _RU[low]
            out.append(t.upper() if ch.isupper() and t else t)
        elif 0x0400 <= ord(ch) <= 0x04FF:   # unmapped Cyrillic glyph -> drop
            continue
        else:
            out.append(ch)
    return _title(re.sub(r"\s+", " ", "".join(out)).strip())


# Latin-script letters with no Unicode decomposition (NFKD won't fold these) -> ASCII.
# Covers Azerbaijani/Turkish (ı, ə) and common European letters, so diacritics FOLD not DROP.
_LATIN_FOLD = str.maketrans({
    "ı": "i", "İ": "I", "ə": "e", "Ə": "E", "ø": "o", "Ø": "O", "ß": "ss", "œ": "oe", "Œ": "OE",
    "æ": "ae", "Æ": "AE", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ħ": "h", "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "Th",
})


def _fold_latin(s):
    """Fold Latin-script diacritics to ASCII (ç->c, ş->s, ğ->g, ü->u, ə->e...) so the name/letter
    is KEPT, not deleted. NFKD splits off combining marks; _LATIN_FOLD handles the non-decomposable
    letters. Must run BEFORE dropping truly non-Latin scripts (CJK etc.)."""
    s = s.translate(_LATIN_FOLD)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def translit_any(s):
    """Best-effort ANY-script -> readable Latin, so nothing non-Latin reaches the CRM.

    Routes by script (Georgian org-words via geo_to_en, Arabic/Persian via fa_translit, Cyrillic
    via ru_translit), then FOLDS Latin diacritics to ASCII (keeps the letter). Any residual truly
    non-Latin glyph (CJK, etc.) is dropped. Returns '' if the string was purely non-mappable."""
    if not s:
        return s or ""
    if s.isascii():                                # already ASCII, nothing to do
        return s.strip()
    s = geo_to_en(s)                                # Georgian (+org-word translation), no-op if none
    s = fa_translit(s)                              # Arabic/Persian, no-op if none
    s = ru_translit(s)                              # Cyrillic, no-op if none
    s = _fold_latin(s)                              # ç/ş/ğ/ü/ə... -> c/s/g/u/e (fold, don't drop)
    s = re.sub(r"[^\x00-\x7f]+", " ", s)            # only NOW drop remaining non-Latin (Chinese, etc.)
    return _title(re.sub(r"\s+", " ", s).strip())
