"""Check human authorization before dispatching model-selected billing changes."""
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


_CONTROL_FIELDS = {
    'action', 'confirmed', 'order_id', 'customer_id', 'execution_mode',
    'execution_customer_id', 'execution_coupon_codes',
}
_AMOUNT_FIELDS = {'amount', 'credit_amount'}
_DECLINE = re.compile(
    r"\b(?:no|not|never|don['’]?t|do\s+not|cannot|can['’]?t|without|stop|cancel\s+that|"
    r"instead|except|unless|if|when|provided|suppose|hypothetical|example|preview|maybe|"
    r"might|perhaps|wait|hold)\b", re.I)
_AFFIRMATIVE = re.compile(
    r"(?:yes(?:\s+(?:please|go ahead|proceed|please proceed|please do|do it|confirm))?|"
    r"ok(?:ay)?(?:\s+(?:please|go ahead|proceed))?|go ahead|please proceed|proceed|"
    r"please do|do it|confirmed|confirm|i confirm|sounds good|that works)", re.I)
_NEGATED_ACTION = re.compile(
    r"\b(?:do\s+not|don['’]?t|never|not|no|without)\s+(?:(?:actually|yet|ever)\s+)?"
    r"(?:approve|authori[sz]e|grant|ship|deliver|dispatch|escalate|flag|waive)\b", re.I)
_CONDITIONAL = re.compile(r'\b(?:if|unless|provided|maybe|perhaps|hypothetical|suppose)\b', re.I)
_GUIDANCE = ('No change was executed. Show the exact proposed action and its details, '
             'then ask for explicit confirmation. Keep those arguments in the saved proposal.')


def _words(value):
    return re.sub(r'\s+', ' ', str(value).casefold()).strip()


def _affirmative(message):
    # Questions, additional instructions and corrections are deliberately not
    # interpreted as approval of an older proposal.
    if not isinstance(message, str) or '?' in message or _DECLINE.search(message):
        return False
    cleaned = re.sub(r'[,!.]+', ' ', message)
    return bool(_AFFIRMATIVE.fullmatch(re.sub(r'\s+', ' ', cleaned).strip()))


def _customer_affirmative(message, proposal):
    """Allow a natural restatement of the saved action, never revised terms."""
    if _affirmative(message):
        return True
    if not isinstance(message, str) or not isinstance(proposal, dict) or '?' in message:
        return False
    action = proposal.get('action')
    for match in _DECLINE.finditer(message):
        if not (match.group().casefold().startswith('cancel') and
                action in ('cancel_draft', 'cancel_order')):
            return False
    if re.search(r'\b(?:and|also|but|different|another|new|more|less|only|rather|with|without)\b', message, re.I):
        return False
    verbs = {
        'place_order': {'place','submit','complete','finalize','buy'},
        'cancel_draft': {'cancel'}, 'cancel_order': {'cancel'},
        'replace_items': {'replace','update','change'},
        'update_customer': {'update','save','change'},
        'process_refund': {'submit','request','file'},
        'apply_store_credit': {'apply','use','reserve'},
        'correct_invoice': {'correct','update','save'},
        'report_billing_issue': {'record','report','submit','flag'},
        'escalate_refund': {'escalate','flag','submit'},
        'bulk_refund': {'submit','request','file'},
    }.get(action, set())
    beginning = re.match(r'^\s*(?:yes|ok(?:ay)?|i confirm|please)\b[,!.\s]*(?:please\s+)?(.+?)\s*[.!]*\s*$', message, re.I)
    if not beginning:
        return False
    tokens = re.findall(r'[A-Za-z0-9_-]+', beginning[1].casefold())
    if not set(tokens) & verbs:
        return False
    numbers = set('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen '
                  'sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand '
                  'first second third fourth fifth sixth seventh eighth ninth tenth'.split())
    if set(tokens) & numbers:
        return False
    target = str(proposal.get('order_id') or '').casefold()
    if any(any(char.isdigit() for char in token) and token != target for token in tokens):
        return False
    known = set(re.findall(r'[A-Za-z0-9_-]+', str(proposal.get('summary', '')).casefold()))
    neutral = set('the that this those these my our your current existing unchanged same specified shown agreed '
                  'proposed exact as is of for to a an please now it details'.split())
    action_domains = {
        'place_order': {'order','cart','basket'},
        'cancel_draft': {'draft','cart','basket'}, 'cancel_order': {'order'},
        'replace_items': {'draft','cart','basket','items'},
        'update_customer': {'profile','account','contact'},
        'process_refund': {'refund','request'},
        'apply_store_credit': {'store','credit','reservation'},
        'correct_invoice': {'invoice','contact'},
        'report_billing_issue': {'billing','issue','report','note'},
        'escalate_refund': {'refund','review','request'},
        'bulk_refund': {'refund','refunds','requests'},
    }
    domains = action_domains.get(action, set())
    subjects = set().union(*action_domains.values())
    if set(tokens) & subjects and not set(tokens) & domains:
        return False
    neutral.update(domains)
    return all(token in known | neutral | verbs | {target} for token in tokens)


def canonical_action_arguments(arguments):
    """Bind proposed values without scope controls or insignificant money formatting.

    Other values retain their JSON types, list order and exact text. In particular,
    a bool must never compare equal to an integer quantity.
    """
    if not isinstance(arguments, dict):
        raise ValueError('Action arguments must be an object.')
    result = {}
    for key, value in arguments.items():
        if not isinstance(key, str):
            raise ValueError('Action argument names must be text.')
        if key in _CONTROL_FIELDS or value is None:
            continue
        if key in _AMOUNT_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
                raise ValueError('Amounts must be finite nonnegative money values.')
            try:
                amount = Decimal(str(value))
                if not amount.is_finite() or not 0 <= amount <= Decimal('99999999.99'):
                    raise ValueError('Amounts must fit DECIMAL(10,2).')
                rounded = amount.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
                if amount != rounded:
                    raise ValueError('Amounts must not contain fractional cents.')
                value = format(rounded, '.2f')
            except InvalidOperation as exc:
                raise ValueError('Amounts must be valid money values.') from exc
        result[key] = value
    try:
        # Round-trip also rejects NaN, non-JSON objects and recursive containers.
        return json.loads(json.dumps(result, allow_nan=False, sort_keys=True))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError('Action arguments must contain finite JSON values.') from exc


def _same_arguments(proposed, current):
    try:
        left = canonical_action_arguments(proposed)
        right = canonical_action_arguments(current)
        return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
    except ValueError:
        return False


def approval_details(arguments):
    """Show every bound value in readable field/value lines, never a clipped preview."""
    bound = canonical_action_arguments(arguments)
    lines = []

    def append(value, label):
        if isinstance(value, dict):
            if not value:
                lines.append(f'{label}: empty')
            for key, child in value.items():
                append(child, f'{label} {key.replace("_", " ")}'.strip())
        elif isinstance(value, list):
            if not value:
                lines.append(f'{label}: none')
            for index, child in enumerate(value, 1):
                append(child, f'{label} item {index}')
        else:
            shown = ('yes' if value else 'no') if isinstance(value, bool) else str(value)
            lines.append(f'{label[:1].upper() + label[1:]}: {shown}')
        if sum(len(line) + 1 for line in lines) > 5000:
            raise ValueError('Proposed action details are too long to show completely. Use a smaller change.')

    for key, value in bound.items():
        append(value, key.replace('_', ' '))
    return '\n'.join(lines)


def _customer_action(name, arguments):
    action = arguments.get('action')
    if name == 'place_order':
        return 'place_order', False
    if name == 'manage_draft':
        mapped = {'replace_items': 'replace_items', 'cancel': 'cancel_order',
                  'apply_store_credit': 'apply_store_credit',
                  'report_billing_issue': 'report_billing_issue'}
        return mapped.get(action), action != 'cancel'
    if name == 'manage_customer' and action == 'update':
        return 'update_customer', True
    if name == 'process_refund':
        mapped = {'request': 'process_refund', 'escalate': 'escalate_refund',
                  'bulk_request': 'bulk_refund'}
        return mapped.get(action or 'request'), True
    if name == 'download_invoice' and action == 'correct_details':
        return 'correct_invoice', True
    return None, False


def customer_action(name, arguments):
    """Identify the saved proposal consumed after a protected customer mutation succeeds."""
    if not isinstance(arguments, dict):
        return None
    action, _ = _customer_action(name, arguments)
    if not action:
        return None
    order_id = None if action in ('update_customer', 'bulk_refund') else arguments.get('order_id')
    return action, order_id


def _selected_draft_requested(message, arguments, shopping_state):
    """Resolve a small ordinal choice only from the actual displayed products."""
    if not isinstance(shopping_state, dict) or not isinstance(arguments, dict):
        return False
    match = re.fullmatch(r'\s*i\s+(?:want|would like|will take)\s+(?:the\s+)?'
                         r'(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)'
                         r'\s+(?:one|item|product)\s*[.!]?\s*', message, re.I)
    if not match:
        return False
    positions = 'first second third fourth fifth sixth seventh eighth ninth tenth'.split()
    displayed = shopping_state.get('displayed_product_ids')
    if not isinstance(displayed, list) or not all(isinstance(value, str) for value in displayed) or len(displayed) != len(set(displayed)):
        return False
    index = positions.index(match[1].casefold())
    if index >= len(displayed):
        return False
    records = shopping_state.get('known_products', [])
    if not isinstance(records, list):
        return False
    known = {row.get('item_code') for row in records
             if isinstance(row, dict) and isinstance(row.get('item_code'), str)}
    selected = displayed[index]
    if not isinstance(selected, str) or selected not in known:
        return False
    quantity = shopping_state.get('quantity')
    quantity = 1 if quantity is None else quantity
    if type(quantity) is not int or quantity < 1:
        return False
    items = arguments.get('items')
    return (isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict)
            and set(items[0]) == {'item_code','quantity'}
            and items[0]['item_code'] == selected
            and type(items[0]['quantity']) is int and items[0]['quantity'] == quantity)


def _draft_requested(message, arguments=None, shopping_state=None):
    if not isinstance(message, str) or _DECLINE.search(message):
        return False
    text = _words(message)
    if _selected_draft_requested(message, arguments, shopping_state):
        return True
    if re.search(r'\b(?:explain|describe|how|what|whether|suppose|tell me about)\b', text):
        return False
    if '?' in message and not re.match(
            r'^(?:(?:can|could|would|will) you (?:please )?|please )?'
            r'(?:create|make|prepare|start|build|request|add|put|quote|order|buy)\b', text):
        return False
    # Only reversible draft creation is allowed directly. This does not place an
    # order, spend store credit or treat a product/price question as consent.
    prefix = (r'^(?:(?:please|kindly)\s+|(?:can|could|would|will) you\s+(?:please\s+)?|'
              r'i\s+(?:want|would like|need)\s+(?:you\s+)?to\s+)?')
    return bool(re.search(prefix + (
        r'(?:(?:create|make|prepare|start|build|request|add|put)\b[^.!?;]{0,100}'
        r'\b(?:draft|quote|quotation|cart|basket|order)\b'
        r'|(?:quote|order|buy)\s+(?!status\b|history\b|number\b|id\b)\S)'), text))


def _mentions_order(text, order_id):
    return bool(isinstance(order_id, str) and order_id and re.search(
        r'(?<![\w-])' + re.escape(order_id) + r'(?![\w-])', text, re.I))


def _other_order(text, order_id):
    """Avoid treating a receipt or a yes for several identified orders as one approval."""
    if not isinstance(order_id, str):
        return True
    prefix = re.match(r'([A-Za-z_-]+)\d', order_id)
    candidates = set(re.findall(r'\border\s+(?:(?:id|number)\s*[:#]?\s*)?'
                                r"['\"]?([A-Za-z0-9][A-Za-z0-9_-]{0,49})", text, re.I))
    if prefix:
        candidates.update(re.findall(r'(?<![\w-])' + re.escape(prefix[1]) + r'[A-Za-z0-9_-]+', text, re.I))
    # Receipt evidence for OTHER-2 must not authorize ORD-1 simply because the
    # identifiers use different prefixes. ISO dates contain no letters.
    candidates.update(token for token in re.findall(r'(?<![\w-])[A-Za-z0-9][A-Za-z0-9_-]{0,49}(?![\w-])', text)
                      if any(char.isalpha() for char in token) and any(char.isdigit() for char in token)
                      and ('-' in token or '_' in token))
    return any(value.casefold() != order_id.casefold() and
               (any(c.isdigit() for c in value) or '-' in value or '_' in value or value.isupper())
               for value in candidates)


def _clauses(text):
    return re.split(r'[;\n]|(?<!\d)\.|\.(?!\d)|\b(?:and|then|but)\b', text, flags=re.I)


def _staff_command(message, action, order_id):
    if not isinstance(message, str):
        return False
    verbs = {'approve': r'(?:approve|authori[sz]e|grant)',
             'escalate': r'(?:escalate|flag)', 'ship': r'(?:ship|dispatch)',
             'deliver': r'(?:deliver|mark\b[^.;\n]{0,100}\bdelivered)'}
    verb = verbs[action]
    prefix = r'^\s*(?:yes\s*[,!]\s*|yes\s+)?(?:(?:please|kindly)\s+|(?:can|could|would|will)\s+you\s+(?:please\s+)?|i\s+)?'
    if any(_mentions_order(clause, order_id) and _NEGATED_ACTION.search(clause)
           for clause in _clauses(message)):
        return False
    # Keep positions so an unqualified later withdrawal cancels an earlier
    # command, while a later new explicit command may replace an earlier "no".
    clauses, start = [], 0
    for separator in re.finditer(r'[;?!\n]|(?<!\d)\.|\.(?!\d)|\b(?:and|then|but)\b', message, re.I):
        clauses.append((start, message[start:separator.start()]))
        start = separator.end()
    clauses.append((start, message[start:]))
    withdrawn = -1
    for position, clause in clauses:
        cleaned = re.sub(r'^\s*(?:actually|on second thought)[,:]?\s*', '', clause, flags=re.I).strip()
        if re.fullmatch(r"(?:no|not yet|never mind|do not(?: do (?:it|that))?|don['’]?t(?: do (?:it|that))?|"
                        r'wait|hold (?:on|off)|stop|cancel that)\s*', cleaned, re.I):
            withdrawn = position
    for position, clause in clauses:
        if position < withdrawn:
            continue
        if _CONDITIONAL.search(clause) or re.search(r'\bwhen\b', clause, re.I):
            continue
        if re.search(r'\bor\b', clause, re.I) and _other_order(clause, order_id):
            continue
        if _mentions_order(clause, order_id) and re.search(prefix + verb + r'\b', clause, re.I):
            if not re.search(r'\b(?:explain|describe|compare|show|tell|whether|should)\b', clause, re.I):
                return True
        if action in ('ship', 'deliver'):
            target = 'shipped' if action == 'ship' else 'delivered'
            if (_mentions_order(clause, order_id) and re.search(
                    prefix + r'mark\b[^.;\n]{0,100}\b' + target + r'\b', clause, re.I)):
                return True
    return False


def _staff_proposal(previous_reply, action, order_id):
    if not isinstance(previous_reply, str) or not _mentions_order(previous_reply, order_id):
        return False
    if _other_order(previous_reply, order_id) or re.search(r'\b(?:if|unless|when|provided|or)\b', previous_reply, re.I):
        return False
    # A prior success statement is not an approval target. The previous assistant
    # message must actually ask for authorization of this identified action.
    if not re.search(r'\b(?:confirm|shall|may i|would you like|do you want|proceed)\b', previous_reply, re.I):
        return False
    if '?' not in previous_reply and not re.search(r'\bplease confirm\b', previous_reply, re.I):
        return False
    verbs = {'approve': r'\b(?:approve|approval|authori[sz]e)\b',
             'escalate': r'\b(?:escalate|escalation)\b',
             'ship': r'\b(?:ship|shipment|shipped)\b',
             'deliver': r'\b(?:deliver|delivery|delivered)\b'}
    return any(_mentions_order(clause, order_id) and re.search(verbs[action], clause, re.I)
               for clause in _clauses(previous_reply))


def _receipt_statement(message):
    return bool(re.search(
        r'\b(?:return(?:ed)?(?:\s+(?:goods|items|products))?|goods|items|products)\b'
        r'(?:\s+\w+){0,5}\s+(?:received|returned)\b'
        r'|\b(?:received|received back)\s+(?:(?:the|these|all|returned)\s+)*'
        r'(?:return|goods|items|products)\b', message, re.I))


def _staff_affirmative(message):
    if _affirmative(message):
        return True
    if not isinstance(message, str) or '?' in message:
        return False
    return bool(re.fullmatch(r'\s*(?:yes|ok|okay)[,\s]+(?:(?:return|goods|items)\s+)?received[.!]?\s*', message, re.I))


def _staff_money_matches(message, arguments):
    """An explicit refund amount cannot silently become the stored request amount."""
    number = r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
    before = r'(?<![\w.,-])'
    after = r'(?![\w,-]|\.\d)'
    patterns = (
        before + r'(?:USD\s*|\$\s*|dollars?\s+)(' + number + r')' + after,
        before + r'(' + number + r')\s*(?:USD\b|dollars?\b)',
        r'\b(?:refund|amount)\s*(?:(?:of|is)\s*|[:=]\s*)?\$?\s*(' + number + r')' + after,
        before + r'(' + number + r')\s+(?:refund|return)\b',
    )
    amounts = [match[1].replace(',', '') for pattern in patterns
               for match in re.finditer(pattern, message, re.I)]
    if not amounts:
        return True
    try:
        actual = canonical_action_arguments(arguments).get('amount')
        return actual is not None and all(Decimal(value) == Decimal(actual) for value in amounts)
    except (ValueError, InvalidOperation):
        return False


def _staff_destination_matches(message, arguments):
    """Honor an explicitly named refund destination and linked target draft."""
    destinations = []
    if re.search(r'\b(?:as|to|in|issue)\s+(?:(?:a|the)\s+)?store\s+credit\b',message,re.I):
        destinations.append('store_credit')
    if re.search(r'\b(?:to|via)\s+(?:the\s+)?original\s+(?:payment\s+)?method\b',message,re.I):
        destinations.append('original_method')
    if any(value != arguments.get('disposition') for value in destinations):
        return False
    targets = re.findall(r'\btarget\s+(?:draft|order)(?:\s+(?:id|number))?\s*[:#]?\s*'
                         r'([A-Za-z0-9][A-Za-z0-9_-]{0,49})\b',message,re.I)
    actual = arguments.get('target_order_id')
    return not targets or isinstance(actual,str) and all(value.casefold() == actual.casefold() for value in targets)


def _human_receipt(message, previous_reply, order_id):
    if re.search(r"\b(?:not|never|no|don['’]?t)\b(?:\s+\w+){0,4}\s+(?:received|returned)\b", message, re.I):
        return False
    if not _other_order(message, order_id) and any(
            _receipt_statement(clause) and '?' not in clause
            and not _CONDITIONAL.search(clause) and not re.search(r'\b(?:when|should)\b', clause, re.I)
            and not re.match(r'\s*(?:have|has|were|are|did|do|does|is)\b', clause, re.I)
            for clause in _clauses(message)):
        return True
    if not (_staff_affirmative(message) and isinstance(previous_reply, str)
            and _mentions_order(previous_reply, order_id)):
        return False
    # A statement that goods arrived cannot be invented by the model and then
    # treated as human evidence. An affirmative must answer a receipt question.
    return any(not _DECLINE.search(clause) and not _other_order(clause, order_id)
        and _receipt_statement(clause) and re.search(
        r'\b(?:confirm|verify|attest|were|have|has|are|did)\b', clause, re.I)
        and ('?' in clause or re.search(r'\bplease confirm\b', clause, re.I))
        for clause in _clauses(previous_reply))


def _human_waiver(message, previous_reply, order_id, reason):
    if not isinstance(reason, str) or not reason.strip() or _NEGATED_ACTION.search(message):
        return False
    source = message
    answering_proposal = _staff_affirmative(message)
    if answering_proposal:
        if not isinstance(previous_reply, str) or not _mentions_order(previous_reply, order_id):
            return False
        source = previous_reply
    if _other_order(source, order_id):
        return False
    if not answering_proposal and any(
            re.search(r'\b(?:waive|waiver)\b', clause, re.I) and
            ('?' in clause or re.search(r'\b(?:should|whether|if|unless)\b', clause, re.I))
            for clause in _clauses(source)):
        return False
    return bool(re.search(r'\b(?:waive|waiver)\b[^.;\n]{0,60}\breturn\b'
                         r'|\breturn\b[^.;\n]{0,60}\bwaiver\b', source, re.I)
                and _words(reason) in _words(source))


def action_error(name, arguments, mode, message, proposal=None, previous_reply=None, shopping_state=None):
    """Return a dispatch rejection; a successful check performs no database work.

    The caller supplies a persisted proposal only after refreshing its fingerprint
    against current records. Private tools independently enforce ownership, return
    windows, pending requests, stock and role limits.
    """
    if not isinstance(arguments, dict) or not isinstance(message, str):
        return _GUIDANCE
    if 'return_received' in arguments and type(arguments['return_received']) is not bool:
        return 'No change was executed. Return receipt must be an explicit true or false value supported by staff confirmation.'
    if mode == 'customer':
        if name == 'request_order':
            return None if _draft_requested(message, arguments, shopping_state) else _GUIDANCE
        expected, bound = _customer_action(name, arguments)
        if not expected:
            return None
        if not isinstance(proposal, dict) or not _customer_affirmative(message, proposal):
            return _GUIDANCE
        actions = {expected, 'cancel_draft'} if expected == 'cancel_order' else {expected}
        if proposal.get('action') not in actions:
            return _GUIDANCE
        order_id = arguments.get('order_id') or None
        if expected == 'correct_invoice' and order_id is None:
            # The tool identifies an issued document by invoice_number; its exact
            # value is still bound below, while the caller refreshed this order.
            order_id = proposal.get('order_id')
        if expected not in ('update_customer', 'bulk_refund') and proposal.get('order_id') != order_id:
            return _GUIDANCE
        if expected in ('update_customer', 'bulk_refund') and proposal.get('order_id') is not None:
            return _GUIDANCE
        if expected == 'cancel_order' and (proposal.get('arguments') is not None or
                arguments.get('reason') not in (None, '')):
            bound = True
        if expected == 'process_refund':
            try:
                amount = canonical_action_arguments(arguments).get('amount')
                if amount is None or Decimal(amount) <= 0:
                    return ('No change was executed. Quote the returned items first and show the exact '
                            'positive refund amount in the proposal before asking for confirmation.')
            except (ValueError, InvalidOperation):
                return _GUIDANCE
        if bound and not _same_arguments(proposal.get('arguments'), arguments):
            return _GUIDANCE
        return None
    if mode not in ('admin', 'cashier'):
        return 'A trusted customer or staff role is required.'
    action = arguments.get('action')
    protected = ((name == 'process_refund' and action in ('approve', 'escalate'))
                 or (name == 'manage_draft' and action in ('ship', 'deliver')))
    if not protected:
        return None
    order_id = arguments.get('order_id')
    if name == 'process_refund' and action == 'approve' and not _staff_money_matches(message, arguments):
        return ('No change was executed. The amount in the staff instruction does not match the pending refund. '
                'Show the exact stored refund amount and ask for confirmation before approval.')
    if name == 'process_refund' and action == 'approve' and not _staff_destination_matches(message, arguments):
        return ('No change was executed. The destination in the staff instruction does not match the pending refund. '
                'Show the exact stored refund destination and ask for confirmation before approval.')
    saved_action = (proposal.get('action', proposal.get('arguments', {}).get('action', 'approve'))
                    if isinstance(proposal, dict) and isinstance(proposal.get('arguments'), dict) else None)
    saved_approval = (isinstance(proposal, dict) and proposal.get('kind') == 'staff_refund'
                      and proposal.get('order_id') == order_id and saved_action == action
                      and _same_arguments(proposal.get('arguments'), arguments))
    approval_reply = previous_reply
    if (saved_approval and isinstance(previous_reply,str) and isinstance(order_id,str)
            and re.match(r'^Please confirm approval of the pending refund for order '
                         + re.escape(order_id) + r': ',previous_reply,re.I)):
        # The exact saved arguments already bind the reason and target draft.
        # Their identifiers are metadata, not alternate approval/receipt targets.
        # Keep every instruction/question line, and never alter the human reply.
        approval_reply = '\n'.join(line for line in previous_reply.splitlines()
            if not re.match(r'^(?:Reason|Destination|Target draft):',line,re.I))
    if not (_staff_command(message, action, order_id) or (saved_approval
            and _staff_affirmative(message) and _staff_proposal(approval_reply, action, order_id))):
        return _GUIDANCE
    if name == 'process_refund' and action == 'approve':
        if arguments.get('return_received') is True and not _human_receipt(message, approval_reply, order_id):
            return 'No change was executed. Ask staff to confirm that the physical returned goods were received.'
        waiver = arguments.get('waiver_reason')
        if waiver:
            if mode != 'admin':
                return 'No change was executed. Only an admin can authorize a documented return waiver.'
            if not _human_waiver(message, previous_reply, order_id, waiver):
                return 'No change was executed. Ask the admin to authorize the exact return waiver and its reason.'
    return None
