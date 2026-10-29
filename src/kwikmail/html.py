"""Convert email HTML into terminal reading order without fetching resources."""

import re
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify

_NON_CONTENT = {
    "head",
    "title",
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "svg",
    "canvas",
    "form",
    "input",
    "button",
    "select",
    "textarea",
    "template",
    "noscript",
}
_TABLE_LAYOUT = {"table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption"}


def _hidden(element: Tag) -> bool:
    styles = {}
    for declaration in str(element.get("style", "")).split(";"):
        name, separator, value = declaration.partition(":")
        if separator:
            styles[name.strip().lower()] = value.lower().split("!", 1)[0].strip()
    return (
        element.has_attr("hidden")
        or styles.get("display") == "none"
        or styles.get("visibility") in {"hidden", "collapse"}
        or styles.get("mso-hide") == "all"
        or styles.get("opacity") == "0"
    )


def to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in reversed(soup.find_all(True)):
        if element.name in _NON_CONTENT or _hidden(element):
            element.decompose()
        elif element.name == "img":
            pixel = all(
                re.fullmatch(r"[01](?:px)?", str(element.get(axis, "")).strip())
                for axis in ("width", "height")
            )
            alt = " ".join(str(element.get("alt", "")).split())
            element.replace_with(soup.new_string(f" {alt} " if alt and not pixel else ""))
        elif element.name in _TABLE_LAYOUT:
            element.name = "div"
        elif element.name == "a":
            href = str(element.get("href", "")).strip()
            try:
                scheme = urlsplit(href).scheme.lower()
            except ValueError:
                scheme = ""
            if scheme in {"http", "https", "mailto"} and not re.search(r"[\x00-\x20]", href):
                element["href"] = quote(href, safe=":/?#[]@!$&'*+,;=%~")
            else:
                element.attrs.pop("href", None)
    return markdownify(str(soup), heading_style="ATX").strip()
