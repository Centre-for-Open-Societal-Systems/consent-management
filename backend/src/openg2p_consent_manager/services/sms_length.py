"""One-SMS-part rule, identical to the OAN Messaging Service's check.

OAN refuses text that does not fit one part, so the OTP text is measured before it is sent.
"""

GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENSION = frozenset("^{}\\[~]|€\f")


def fits_one_sms(text: str) -> bool:
    """Same rule OAN enforces: 160 GSM-7 septets (extension chars = 2), else 70 UTF-16."""
    if all(c in GSM7_BASIC or c in GSM7_EXTENSION for c in text):
        return sum(2 if c in GSM7_EXTENSION else 1 for c in text) <= 160
    return len(text.encode("utf-16-le")) // 2 <= 70
