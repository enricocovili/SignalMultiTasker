"""Forwarding rules: noisy, predictable senders get a one-line notice."""

import logging

logger = logging.getLogger(__name__)

# A rule short-circuits normal forwarding: when an email matches, we send the
# rule's own message instead of the full "New Email" forward. This keeps noisy,
# predictable senders (security alerts, login links) to a one-line notice and
# skips the LLM entirely.
#
# Each rule is a dict:
#   {
#     "name": "<human label, for logs only>",
#     "match": { "<condition>": "<value>", ... },   # ALL conditions must hold (AND)
#     "message": "<Signal message to send when matched>",
#   }
#
# Supported match conditions (see MATCHERS). All are case-insensitive. To add a
# new condition, register a predicate in MATCHERS; to add a new rule, append to
# RULES. Rules are evaluated top-to-bottom; the first match wins.
#
#   sender_contains  — substring of the decoded From header (name <addr>)
#   sender_equals    — exact match of the parsed email address only
#   subject_contains — substring of the decoded Subject header
#   subject_equals   — exact match of the whole Subject
MATCHERS = {
    "sender_contains": lambda ctx, v: v.casefold() in ctx["sender"].casefold(),
    "sender_equals": lambda ctx, v: v.casefold() == ctx["sender_addr"].casefold(),
    "subject_contains": lambda ctx, v: v.casefold() in ctx["subject"].casefold(),
    "subject_equals": lambda ctx, v: v.casefold() == ctx["subject"].casefold(),
}

RULES = [
    {
        "name": "google-security-alert",
        "match": {
            "sender_equals": "no-reply@accounts.google.com",
            "subject_contains": "Avviso di sicurezza",
        },
        "message": (
            "🔐 **Google account** — a new security event was reported "
            "(*Avviso di sicurezza*). Check the account activity."
        ),
    },
    {
        "name": "anthropic-login-link",
        "match": {
            "sender_contains": "mail.anthropic.com",
            "subject_contains": "Your secure link to Claude.ai",
        },
        "message": "🔑 **Claude.ai** — a new login link was requested for your account.",
    },
    {
        "name": "cloudflare-login-token",
        "match": {
            "sender_equals": "noreply@notify.cloudflare.com",
            "subject_contains": "Your Cloudflare login token",
        },
        "message": "🔑 **Cloudflare** — a new login token was requested for your account.",
    },
]


def match_rule(sender_addr, sender, subject):
    """Return the first RULES entry matching this email, or None.

    All conditions in a rule's ``match`` dict must hold. Unknown conditions make
    a rule never match (fail closed) so a typo can't silently forward nothing.
    """
    ctx = {"sender": sender, "sender_addr": sender_addr, "subject": subject}
    for rule in RULES:
        try:
            if all(
                cond in MATCHERS and MATCHERS[cond](ctx, value)
                for cond, value in rule["match"].items()
            ):
                return rule
        except Exception as e:
            logger.warning("Error evaluating rule %r: %s", rule.get("name"), e)
    return None
