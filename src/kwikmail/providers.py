"""Protocol defaults; explicit account settings always win."""

PROVIDERS = {
    "gmail": {"host": "imap.gmail.com", "smtp_host": "smtp.gmail.com", "sent_folder": ""},
    "fastmail": {"host": "imap.fastmail.com", "smtp_host": "smtp.fastmail.com"},
    "icloud": {"host": "imap.mail.me.com", "smtp_host": "smtp.mail.me.com"},
    "yahoo": {"host": "imap.mail.yahoo.com", "smtp_host": "smtp.mail.yahoo.com", "sent_folder": ""},
    "zoho": {"host": "imap.zoho.com", "smtp_host": "smtp.zoho.com"},
    "outlook": {
        "host": "outlook.office365.com",
        "smtp_host": "smtp-mail.outlook.com",
        "auth": "oauth2",
    },
    "microsoft365": {
        "host": "outlook.office365.com",
        "smtp_host": "smtp.office365.com",
        "auth": "oauth2",
    },
}
ZOHO_REGIONS = {"us": "com", "eu": "eu", "in": "in", "au": "com.au", "jp": "jp", "ca": "ca"}


def account_defaults(data: dict) -> dict:
    provider = data.get("provider", "")
    if not isinstance(provider, str) or (provider and provider not in PROVIDERS):
        raise ValueError(
            "Unknown provider; use gmail, fastmail, icloud, yahoo, zoho, outlook or microsoft365"
        )
    if "zoho_region" in data and provider != "zoho":
        raise ValueError("zoho_region requires provider = 'zoho'")
    if not provider:
        return data
    defaults = {
        "imap_port": 993,
        "imap_security": "tls",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "sent_folder": "auto",
        **PROVIDERS[provider],
    }
    if provider == "zoho":
        region = data.get("zoho_region", "us")
        if not isinstance(region, str) or region not in ZOHO_REGIONS:
            raise ValueError("zoho_region must be us, eu, in, au, jp or ca")
        suffix = ZOHO_REGIONS[region]
        defaults.update(host=f"imap.zoho.{suffix}", smtp_host=f"smtp.zoho.{suffix}")
    address = data.get("address")
    if isinstance(address, str):
        defaults["username"] = address
        if provider == "icloud":
            defaults["username"] = address.split("@", 1)[0]
            defaults["smtp_username"] = address
    return defaults | data
