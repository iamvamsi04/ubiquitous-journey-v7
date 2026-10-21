"""Coupon evidence comes from customer messages, never model suggestions or SQL."""
import re

TOKEN = re.compile(r'(?<![\w-])[A-Za-z0-9][A-Za-z0-9_-]{1,49}(?![\w-])')
GENERIC = set('all any available best better biggest cheapest code codes coupon coupons discount discounts '
              'highest largest maximum max minimum most none offer offers one promotion promotions promo '
              'redeem same that the this use voucher vouchers yes'.split())
NEGATIVE = re.compile(r"\b(?:do not|don['’]t|never|without|remove|forget|skip|no coupons?|no discounts?)\b", re.I)
NO_WRITE = re.compile(
    r"\b(?:do not|don['’]t|never)\s+(?:actually\s+)?(?:change|modify|update|touch)\b"
    r'|\b(?:as an? example|for illustration|hypothetical|preview only|only a preview)\b', re.I)


def named_orders(message):
    """Honor explicit order/draft identifiers without interpreting ordinary nouns as IDs."""
    values = re.findall(
        r'\b(?:order|draft|quote|cart|basket|for|to|on)(?:\s+|\s*[:#=]\s*)'
        r'(?=(?:(?:id|number)\s*[:#]?\s*|#\s*)?'
        r"['\"]?([A-Za-z0-9][A-Za-z0-9_-]{1,49}))", message, re.I)
    ordinary = GENERIC | set('my our your current existing new latest previous first second active '
                            'open pending order draft quote cart basket both please with'.split())
    return {value for value in values if value.casefold() not in ordinary and (
        any(c.isdigit() for c in value) or '-' in value or '_' in value or value.isupper())}


def clauses(text):
    return re.split(r'[,;.!?\n]|\bbut\b', text)


def supplied_codes(text):
    codes = set()
    for match in TOKEN.finditer(text):
        token = match.group()
        if token.casefold() in GENERIC or not any(c.isalpha() for c in token):
            continue
        prefix = text[max(0, match.start()-35):match.start()]
        labelled = re.search(r'\b(?:coupon|promo|promotion|voucher|code|apply|redeem|use)\s*[:=]?\s*[\'\"]?$', prefix, re.I)
        if labelled or any(c.isdigit() for c in token) or '_' in token or '-' in token or token.isupper():
            codes.add(token.upper())
    return codes


def customer_coupon_codes(message, history, saved=()):
    known = {value.upper() for value in saved if isinstance(value, str) and TOKEN.fullmatch(value)}
    texts = [entry['content'] for entry in history if entry.get('role') == 'user' and isinstance(entry.get('content'), str)]
    for text in [*texts, message]:
        for clause in clauses(text):
            found = supplied_codes(clause)
            if NEGATIVE.search(clause):
                if found:
                    known.difference_update(found)
                elif re.search(r'\b(?:coupon|promo|voucher|discount)', clause, re.I):
                    known.clear()
            else:
                known.update(found)
    return sorted(known)[-30:]


def may_apply_coupon(code, order_id, message, known, proposal=None):
    if not isinstance(code, str) or code.upper() not in known:
        return False
    targets = named_orders(message)
    if NO_WRITE.search(message) or (targets and targets != {order_id}):
        return False
    escaped = re.escape(code)
    boundary = rf'(?<![\w-]){escaped}(?![\w-])'
    targets = set()
    for clause in clauses(message):
        if not NEGATIVE.search(clause):
            targets.update(value.upper() for value in re.findall(
                r'\b(?:apply|redeem|use)\s+(?:(?:the|my|coupon|promo|promotion|voucher|code)\s+)*[\'\"]?([A-Za-z0-9][A-Za-z0-9_-]{1,49})', clause, re.I)
                if value.upper() in known)
    if len(targets) > 1:
        return False
    for clause in clauses(message):
        if not re.search(boundary, clause, re.I) or NEGATIVE.search(clause):
            continue
        # Two proposed codes are a choice to clarify, not permission to pick the best.
        more = re.search(boundary + r'\s+(?:and|or)\s+([A-Za-z0-9][A-Za-z0-9_-]{1,49})', clause, re.I)
        if more and more[1].upper() in supplied_codes(clause):
            continue
        direct = rf'\b(?:apply|redeem|use)\s+(?:(?:the|my|coupon|promo|promotion|voucher|code)\s+)*[\'\"]?{boundary}'
        quoted = rf'\bwith\s+(?:(?:the|my|coupon|promo|code)\s+)*[\'\"]?{boundary}'
        request = re.search(direct, clause, re.I)
        if request:
            # Mentioning an action inside a question is not permission to do it.
            # Examine only its preamble so "apply CODE and explain the saving"
            # still permits the explicitly requested change.
            preamble = clause[:request.start()]
            informational = re.search(
                r'\b(?:what|how|if|whether|should|check|valid|explain|tell|before|when|unless)\b',
                preamble, re.I)
            conditional = re.search(r'\b(?:if|unless|provided|preview|example)\b', clause[request.end():], re.I)
            if not informational and not conditional:
                return True
        if (re.search(quoted, clause, re.I) and re.search(r'\b(?:quote|cart|basket|order)\b', clause, re.I)
                and not re.search(r'\b(?:what|how|would|compare|check|explain|valid)\b', clause, re.I)):
            return True
    # A plain yes is meaningful only for the saved, unchanged coupon proposal.
    if proposal and proposal.get('action') == 'apply_promotion' and proposal.get('order_id') == order_id:
        same = str(proposal.get('promo_code', '')).casefold() == code.casefold()
        affirmative = re.fullmatch(r'\s*(?:yes|yes please|apply it|use it|go ahead|please do)[.!]?\s*', message, re.I)
        return bool(same and affirmative)
    return False
