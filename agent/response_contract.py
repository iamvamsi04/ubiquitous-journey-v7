"""Current-turn retrieval requirements and verified report presentation.

The contract selects evidence and output shape. It never invents business facts,
executes a mutation or treats a prior conversation answer as a fresh read.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date
import re
import unicodedata


READ_TOOLS = frozenset({'execute_sql_query', 'billing_analysis', 'billing_anomalies',
                        'compare_products', 'find_cheaper_alternatives'})
FILTER_KEYS = ('customer_id', 'order_id', 'date_from', 'date_to')
CATEGORY_EXPRESSION = "COALESCE(NULLIF(TRIM(d.category),''),'Uncategorised')"
INVENTORY_QUERY = (f'SELECT {CATEGORY_EXPRESSION} AS category, COUNT(*) AS products, '
    'SUM(CASE WHEN i.availablequantity>0 THEN 1 ELSE 0 END) AS in_stock_products '
    'FROM items i LEFT JOIN product_details d ON d.item_code=i.item_code '
    f'GROUP BY {CATEGORY_EXPRESSION} ORDER BY category')


def words(text):
    text = unicodedata.normalize('NFKC', str(text)).casefold()
    return re.sub(r'\s+', ' ', re.sub(r'[^\w?]+', ' ', text).replace('_', ' ')).strip()


@dataclass(frozen=True)
class ResponseContract:
    kind: str = 'conversation'
    scoped: bool = False
    domains: frozenset[str] = frozenset()
    filter_items: tuple[tuple[str, str], ...] = ()
    chart_mode: str = 'overview'

    @property
    def needs_evidence(self):
        return self.kind != 'conversation'

    @property
    def report(self):
        return self.kind in ('inventory_overview', 'store_overview')

    @property
    def tool(self):
        return {'inventory_overview': 'execute_sql_query', 'store_overview': 'billing_analysis'}.get(self.kind)

    @property
    def expected_filters(self):
        return {key: dict(self.filter_items).get(key) for key in FILTER_KEYS}

    def prompt(self):
        if self.kind == 'inventory_overview':
            return ('\nCURRENT REQUEST CONTRACT: complete product-range overview. Execute '
                'execute_sql_query with this complete read, without extra filters, joins or limits: '
                + INVENTORY_QUERY + '. The application displays all categories as a table and '
                'computes the summary from these rows; no prose formatting call is needed.\n')
        if self.kind == 'store_overview':
            filters = ', '.join(f'{key}={value!r}' for key, value in self.expected_filters.items())
            presentation = f", chart_mode={self.chart_mode!r}" if self.chart_mode != 'overview' else ''
            return ('\nCURRENT REQUEST CONTRACT: comprehensive store analytics. Call '
                "billing_analysis(report='store_overview', " + filters + presentation + '). '
                'These are the exact requested filters; preserve them and add none. Do not replace '
                'the report with a trend, top ten or one metric. The application displays verified '
                'summary tables, charts and complete report datasets; no prose formatting call is needed.\n')
        if self.needs_evidence:
            return ('\nCURRENT REQUEST CONTRACT: retrieve current business data, not an explanation '
                'of how to retrieve it. Use current-turn read tools covering '
                + (', '.join(sorted(self.domains)) or 'the requested records') + '. '
                'Successful results may be empty; schema inspection or an unrelated read is not an answer. '
                'Preserve every explicit filter and do not invent a filter. Tables are attached from '
                'tool rows automatically; explain findings briefly without repeating every table row.\n')
        return ''


def _today():
    return datetime.now(timezone.utc).date()


def _period_bounds(period, previous):
    today = _today()
    if period == 'week':
        first = today - timedelta(days=today.weekday() + 7*int(previous))
        return first, first + timedelta(days=6)
    if period == 'month':
        first = today.replace(day=1)
        if previous:
            first = (first - timedelta(days=1)).replace(day=1)
        after = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        return first, after - timedelta(days=1)
    if period == 'quarter':
        first = today.replace(month=(today.month-1)//3*3+1, day=1)
        if previous:
            prior = first - timedelta(days=1)
            first = prior.replace(month=(prior.month-1)//3*3+1, day=1)
        month = first.month + 3
        after = date(first.year + int(month > 12), (month-1) % 12 + 1, 1)
        return first, after - timedelta(days=1)
    year = today.year - int(previous)
    return date(year, 1, 1), date(year, 12, 31)


def _report_filters(message):
    """Resolve explicit supported scopes; unknown names/references need flexible reads."""
    remaining, selected, ambiguous = str(message), {}, False
    for key, label in (('customer_id', 'customer'), ('order_id', 'order')):
        pattern = re.compile(r'\b' + label + r'(?:[ _]+id)?\s*(?:[:=#]\s*)?\s+([A-Za-z0-9_-]{1,50})\b', re.I)
        matches = list(pattern.finditer(remaining))
        ids = [m[1] for m in matches if re.search(r'\bid\b|_id\b', m[0], re.I)
               or re.search(r'\d|[_-]', m[1])]
        if len(ids) > 1:
            ambiguous = True
        elif ids:
            selected[key] = ids[0]
            for match in reversed(matches):
                if match[1] == ids[0]:
                    remaining = remaining[:match.start()] + ' ' + remaining[match.end():]
    iso = r'(\d{4}-\d{2}-\d{2})'
    ranges = [
        (rf'\b(?:from|between)\s+{iso}\s+(?:to|and|through|until)\s+{iso}', ('date_from','date_to')),
        (rf'\b(?:since|from|date_from\s*[:=]?)\s+{iso}', ('date_from',)),
        (rf'\b(?:through|until|date_to\s*[:=]?)\s+{iso}', ('date_to',)),
        (rf'\b(?:on|for)\s+{iso}', ('date_from','date_to')),
    ]
    for pattern, keys in ranges:
        for match in list(re.finditer(pattern, remaining, re.I))[::-1]:
            try:
                values = [date.fromisoformat(value).isoformat() for value in match.groups()]
            except ValueError:
                ambiguous = True
                continue
            if len(values) == 1 and len(keys) == 2:
                values *= 2
            for key, value in zip(keys, values):
                if key in selected and selected[key] != value:
                    ambiguous = True
                selected[key] = value
            remaining = remaining[:match.start()] + ' ' + remaining[match.end():]
    relative = list(re.finditer(r'\b(?:(today|yesterday)|(?:(this|last|previous)\s+(week|month|quarter|year))|last\s+(\d+)\s+days?)\b', remaining, re.I))
    if len(relative) > 1 or (relative and any(key in selected for key in ('date_from','date_to'))):
        ambiguous = True
    elif relative:
        match = relative[0]
        if match[1]:
            start = end = _today() - timedelta(days=int(match[1].casefold() == 'yesterday'))
        elif match[4]:
            count = int(match[4])
            if not 1 <= count <= 36600:
                return (), True
            start, end = _today() - timedelta(days=count-1), _today()
        else:
            start, end = _period_bounds(match[3].casefold(), match[2].casefold() != 'this')
        selected.update(date_from=start.isoformat(), date_to=end.isoformat())
        remaining = remaining[:match.start()] + ' ' + remaining[match.end():]
    if selected.get('date_from', '') > selected.get('date_to', '9999-12-31'):
        ambiguous = True
    text = words(remaining)
    text = re.sub(r'\b(?:order totals?|customer summaries|for (?:the )?(?:whole |entire )?store)\b', '', text)
    text = re.sub(r'\bfor(?=\s*(?:$|and\b))', '', text)
    unresolved = re.search(r'\b(?:for|from|since|between|customer|order|today|yesterday|last|previous|weeks?|months?|quarters?|years?|'
        r'january|february|march|april|may|june|july|august|september|october|november|december|'
        r'currency|usd|eur|gbp|inr|category|product|brand|except|exclude|excluding|without|omit|omitting|only|not|'
        r'pending|overdue|cheapest|expensive|bought|purchased|email|addresses?|phone|contact|'
        r'waiting|awaiting|status|cancelled|drafts?|placed|issued|fulfilled|delivered|shipped|backorders?)\b|\d', text)
    return tuple((key, selected[key]) for key in FILTER_KEYS if key in selected), bool(ambiguous or unresolved)


def _abstract_comparison(text):
    if not re.search(r'\b(?:compare|comparison|difference|differences|versus|vs)\b',text):
        return False
    topics = {}
    patterns = {'draft':r'drafts?(?: orders?)?', 'placed':r'placed orders?',
        'invoice':r'invoices?', 'refund':r'refunds?(?: requests?| approvals?)?',
        'credit':r'store credit', 'percent':r'percent(?:age)?(?: discounts?)?',
        'flat':r'flat discounts?', 'cancellation':r'order cancellation',
        'customer':r'customer(?: modes?)?', 'cashier':r'cashier(?: modes?)?', 'admin':r'admin(?: modes?)?'}
    remaining = text
    for key,pattern in patterns.items():
        if re.search(r'\b(?:'+pattern+r')\b',remaining):
            topics[key] = True
            remaining = re.sub(r'\b(?:'+pattern+r')\b',' ',remaining)
    remaining = re.sub(r'\b(?:explain|describe|please|what|is|are|the|a|an|between|and|or|versus|vs|compare|comparison|difference|differences|in|as|table|tabular|form|their|permissions|roles|modes|responsibilities|access)\b',' ',remaining)
    if remaining.strip(' ?'):
        return False
    kinds = set(topics)
    if kinds <= {'customer','cashier','admin'}:
        return len(kinds)>=2 and bool(re.search(r'\b(?:permissions|roles|modes|responsibilities|access)\b',text))
    pairs = ({'draft','placed'},{'draft','invoice'},{'refund','credit'},
             {'percent','flat'},{'cancellation','refund'})
    return any(pair <= kinds for pair in pairs)


def _conceptual(text):
    if _abstract_comparison(text):
        return True
    if re.search(r'\b(?:chart|graph|plot) types\b|\btypes of (?:charts|graphs|plots)\b', text) and not re.search(
            r'\b(?:current|actual|our|store|this|these|those|matching) data\b|\bgenerate|draw|render\b', text):
        return True
    policy = re.search(r'\b(?:return window|return policy|refund policy|coupon policy|'
                       r'approval threshold|cashier limit|cashier refund limit|refund limit)\b', text)
    records = re.search(r'\b(?:my|this|that|these|those) (?:orders?|invoices?|refunds?|purchases?)\b|'
                        r'\b(?:show|list|find|which|check)\b.*\b(?:orders?|invoices?|requests?)\b|'
                        r'\b(?:order|invoice|customer) (?:id |number )?[a-z_]*\d|\d{4} \d{2} \d{2}', text)
    if policy and not records:
        # These configured rules are already trusted per-turn system context.
        # Do not force an unrelated database read just to explain a policy.
        return True
    if re.search(r'\b(?:how to|how do i|how can i|how does|what does .+ mean)\b', text):
        return True
    if re.search(r'\b(?:examples?|hypothetical|sql query|prompt|source code|setup steps|implementation|table schema)\b', text):
        return True
    abstract = (r'(?:store wide analytics|storewide analytics|analytics|billing analysis(?: tool)?|'
        r'billing anomalies(?: tool)?|billing agent|a draft|an invoice|a coupon|a promotion|'
        r'order status|return window|refund policy|return policy|password hashes?|failed attempts|locked until)')
    if re.fullmatch(r'(?:what (?:is|are|does)|explain|describe|define) (?:the )?' + abstract + r'(?: work| do| mean| for)?\??', text):
        return True
    live = re.search(r'\b(?:my|our|current|today|yesterday|this|last|total|totals|status)\b|\d', text)
    if not live:
        if re.search(r'\b(?:examples?|definitions?|checklist|best practices|useful questions|shopping tips|steps to|steps for)\b',text):
            return True
        concepts = set(re.findall(r'\b(?:drafts?|placed orders?|invoices?|refund requests?|refund approvals?|percent discounts?|flat discounts?|store credit)\b',text))
        roles = set(re.findall(r'\b(?:customer|cashier|admin)\b',text))
        if (len(concepts)>=2 and re.search(r'\b(?:difference|differences|definitions?|meanings?)\b',text)
                or len(roles)>=2 and re.search(r'\b(?:permissions|roles|modes|responsibilities|access)\b',text)):
            return True
    if re.match(r'^(?:explain|describe|define|why)\b', text) and not live:
        return True
    return bool(re.search(r'\b(?:refund|return) policy\b', text) and not live)


def _domains(text):
    domains = set()
    if re.search(r'\b(?:products?|items?|categories|stock|inventory|prices?|cheaper|alternatives|features?|specifications?|sku)\b', text):
        domains.add('products')
    if re.search(r'\b(?:orders?|drafts?|fulfillment|delivery|shipment)\b', text):
        domains.add('orders')
    if re.search(r'\b(?:invoices?|billed)\b', text):
        domains.add('invoices')
    if re.search(r'\b(?:refunds?|returns?)\b', text):
        domains.add('refunds')
    if 'refunds' in domains and re.search(r'\b(?:requests?|pending|awaiting)\b', text):
        domains.add('refund_requests')
    if re.search(r'\b(?:sales|revenue|costs?|spending|discounts?|tax|shipping|margin|profit|analytics)\b', text):
        domains.add('finance')
    product_cost = (re.search(r'\b(?:item\d+|sku\d+)\b', text)
        or re.search(r'\bwhat (?:does|do) .+ costs?\b', text))
    if product_cost and not domains & {'orders', 'invoices'} and not re.search(r'\b(?:sales|revenue|spending|tax|shipping|margin|profit|analytics)\b', text):
        domains.add('products')
        domains.discard('finance')
    if re.search(r'\bcredit\b', text):
        domains.add('credit')
    if re.search(r'\b(?:anomalies|inconsistencies|discrepancies)\b', text):
        domains.add('anomalies')
    customer_records = re.search(
        r'\b(?:customers?\s+(?:records?|accounts?|list|directory)|'
        r'(?:list|directory)\s+(?:of\s+)?(?:all\s+)?customers)\b', text)
    if customer_records or re.search(r'\b(?:customers?|account|email|contact)\b', text) and (
            not domains or re.search(r'\b(?:email|contact|details|addresses|profiles|names)\b', text)):
        domains.add('customers')
    return frozenset(domains)


def _named_chart_style(text):
    # Retired styles remain recognizable requests, so they cannot turn into an
    # unrelated dashboard. Availability comes from the chart contract.
    return re.search(r'\b(?:bar|pie|donut|scatter|histogram|boxplot|box plot|heatmap|heat map|'
        r'waterfall|water fall|candlestick|candle stick|pareto|funnel|treemap|tree map|timeline|'
        r'bubble|area|combo|combination|step line|range chart|range plot|line chart|line plot)\b|'
        r'\b(?:as|using) (?:a |an )?(?:line|range)\b|'
        r'\b(?:in|into) (?:a|an) (?:line|range)\b', text)


def chart_intent(message):
    """Presentation intent, separate from business measure and access scope."""
    text = words(message)
    if not re.search(r'\b(?:charts?|graphs?|plots?|visuals?|visually|graphical|graphically|visualizations?|visualisations?|'
                     r'visualize|visualise|pictorial|dashboard)\b|\bdraw\b.*\b(?:it|that|them|those|results)\b', text):
        return None
    all_types = re.search(r'\b(?:all|every|each)\s+(?:(?:\d+|the|possible|available|supported|applicable|'
        r'suitable|different|types?|kinds?|of)\s+)*(?:charts?|graphs?|plots?|visualizations?)\b', text)
    return 'all' if all_types else 'dashboard'


def table_intent(message):
    text = words(message)
    if re.search(r'\b(?:no|without|do not|don t) (?:a |any |use )?tables?\b',text):
        return False
    return bool(re.search(r'\b(?:tables?|tabular|rows and columns|side by side)\b',text))


def _record_request(message):
    """Explicit record scope takes priority over an 'explain' or 'how' prefix."""
    text = words(message)
    raw = unicodedata.normalize('NFKC',str(message))
    enumeration = (r'\b(?:all|every|list|show|find|get|give|reveal|display|enumerate|which)\b.*'
                   r'\b(?:(?:coupon|promo|promotion) codes?|coupons|promotions)\b')
    explicit_codes = re.search(r'\b(?:all|every|list|show|find|get|give|reveal|display|enumerate|which)\b.*\b(?:coupon|promo|promotion|discount) codes?\b',text)
    if explicit_codes or re.search(enumeration,text) and not re.search(r'\b(?:policy|policies|definitions?|meanings?|types)\b',text):
        return True
    if re.search(r'\b(?:order|invoice|customer|item|sku)(?:\s+(?:id|number))?\s+[\w]*[-_][\w-]+\b',raw,re.I):
        return True
    if re.search(r"\b\w+['’]s\b.*\b(?:orders?|invoices?|refunds?|accounts?)\b",raw,re.I):
        return True
    generic = {'mode','role','account','record','verification','profile','registration','privacy','details',
               'status','total','types','process','number','id','date','history','request','approval',
               'can','could','will','would','should','must','does','is','are','and','or','versus',
               'permissions','roles','responsibilities','access','cancellation','amount','amounts',
               'line','lines','document','documents','service','support','password','passwords','contact','email'}
    for match in re.finditer(r'\b(?:customer|order|invoice)(?:\s+(?:id|number))?\s+(\w+)\b',raw,re.I):
        if match[1].casefold() not in generic:
            return True
    if re.search(r'\b(?:return window|refund policy|return policy|coupon policy|approval threshold|cashier limit|cashier refund limit|refund limit)\b',text) and _conceptual(text):
        return False
    if re.search(r'\b(?:how to|how do i|how can i|show me how)\b',text):
        return False
    lookup = re.search(r'\b(?:show|list|display|find|search|fetch|check|get|compare|recommend|suggest|explain|describe|what|which|how much|how many|when|where|status|availability)\b|^(?:is|are|does|do)\b',text)
    if lookup and _domains(text) and re.search(r'\b(?:my|our|current|today|yesterday|pending|recorded|overdue|available|in stock)\b',text):
        return True
    if (lookup and re.search(r'\d',text) and _domains(text) and
            not re.search(r'\b(?:examples?|hypothetical|assume|illustrate|definitions?)\b',text)):
        return True
    if _abstract_comparison(text):
        return False
    if re.search(r'\b(?:coupons?|promotions?|(?:coupon|promo|promotion|discount) codes?)\b',text) and not re.search(
            r'\b(?:policy|policies|definitions?|meanings?|means?|types|how .* work|what is a)\b',text):
        return True
    records = re.search(r'\b(?:orders?|invoices?|refund requests?|customers?)\b',text)
    if records and re.search(r'\bfor\s+(?!(?:a|an|the|beginners|businesses|customers|staff|example)\b)\w+',text):
        return True
    definition = re.search(r'\b(?:differences?|definitions?|meanings?|permissions|roles|modes|process|steps|how to|how do|how can)\b',text)
    return bool(records and not definition and re.search(r'\b(?:show|list|find|which|have|overdue|by|all|every|compare|versus)\b',text))


def general_explanation(message):
    """A model-written table is explanatory text, never evidence of store records."""
    text = words(message)
    if _record_request(message) or re.search(r'\b(?:and|then) (?:please )?(?:approve|reject|apply|redeem|update|delete|remove|ship|deliver|buy|purchase|checkout|cancel)\b',text):
        return False
    if _conceptual(text):
        return True
    if re.search(r'\b(?:approve|reject|apply|redeem|register|update|delete|remove|add|ship|deliver|buy|purchase|checkout|cancel|coupon codes?|promo codes?|promotion codes?)\b',text):
        return False
    return not _domains(text)


def _chart_report(message, analytics_context, as_table=False):
    """Resolve broad visuals and explicit references to the last verified report."""
    text = words(message)
    presentation = 'none' if as_table else chart_intent(message)
    broad = re.search(r'\b(?:analytics|dashboard|performance overview|business overview|store overview|(?:store|shop|business) (?:wide )?(?:metrics|statistics|summary))\b', text)
    reference = re.search(r'\b(?:it|that|those|them|these|same|above|previous|earlier|results)\b|'
                          r'\bthis(?:$| (?:in|as|into|on|report|data|analysis|analytics|results?)\b)', text)
    prior = analytics_context if isinstance(analytics_context, dict) else {}
    repeat = re.search(r'\b(?:same|repeat|redo)\b', text)
    if not presentation and reference and repeat and prior.get('kind') == 'store_overview':
        presentation = prior.get('chart_mode', 'overview')
    inherited = prior.get('kind') == 'store_overview' and presentation and (
        reference or not broad and (len(text.split()) <= 6 or
            re.search(r'\b(?:current|available|matching) data\b', text)))
    if not broad and presentation != 'all' and not inherited:
        return None
    # Explicit metrics, chart types or narrower dimensions need the flexible SQL
    # path. A named type must not be silently replaced by a whole dashboard.
    focused = re.search(r'\b(?:only|just|top|trend|compare|comparison|anomalies|by)\b', text)
    if _named_chart_style(text) or focused and not re.search(r'\b(?:comprehensive|everything|complete dashboard)\b', text):
        return None
    filter_message = re.sub(r'\b((?:all|every|each)\s+(?:the\s+)?)\d+\s+(charts?|graphs?|plots?)\b',
                            r'\1\2', str(message), flags=re.I)
    filter_message = re.sub(r'\b(?:for|of|on|with)\s+(?:(?:those|these|that|this|the|same|previous|current)\s+)?'
                            r'(?:results?|data|report|analytics)\b', ' ', filter_message, flags=re.I)
    filters, unresolved = _report_filters(filter_message)
    if unresolved:
        return None
    selected = dict(filters)
    if inherited and not re.search(r'\b(?:whole|entire|full) (?:store|shop|business)\b|\bstore wide\b', text):
        saved = prior.get('filters', {})
        if not isinstance(saved, dict):
            return None
        merged = {key: saved.get(key) for key in FILTER_KEYS}
        if 'customer_id' in selected and selected['customer_id'] != merged['customer_id']:
            merged['order_id'] = None
        merged.update(selected)
        if (merged.get('date_from') or '') > (merged.get('date_to') or '9999-12-31'):
            return None
        filters = tuple((key, value) for key, value in merged.items() if value is not None)
    return ResponseContract('store_overview', bool(filters), frozenset({'finance'}),
                            filters, presentation or 'overview')


def request_contract(message, mode, customer_id=None, analytics_context=None, previous_message=None):
    text = words(message)
    text = re.sub(r'^(?:(?:please|can you|could you|would you)\s+)+', '', text)
    reference = bool(re.search(r'\b(?:it|that|this|those|them|these|same|above|previous|earlier|results)\b',text))
    records = _record_request(message)
    if table_intent(message) and not chart_intent(message) and (not _conceptual(text) or reference or records):
        if mode == 'admin':
            report = _chart_report(message,analytics_context,as_table=True)
            if report is not None:
                return report
        if reference and previous_message and not _domains(text):
            prior = request_contract(previous_message,mode,customer_id)
            if prior.needs_evidence:
                return prior if prior.report else ResponseContract('data',domains=prior.domains)
    if _conceptual(text) and not records:
        return ResponseContract()
    if (not records and re.match(r'^(?:remember\b|my\b|i (?:need|want|prefer)\b)',text)
            and re.search(r'\b(?:budget|preferences?|preferred features|needs?|quantity)\b',text)
            and not re.search(r'\b(?:show|list|find|search|check|get|compare|recommend|suggest)\b',text)):
        return ResponseContract()
    if re.match(r'^(?:request|submit)\b(?!\s+(?:status|history|details)\b).*\b(?:refund|return|quote|order|cart|draft)\b',text):
        return ResponseContract()
    if (re.fullmatch(r'(?:(?:my|the|an?) )?(?:invoice|proforma)(?: please)?',text)
            or re.match(r'^(?:send|provide|get|prepare|generate)\b.*\b(?:invoice|proforma)\b',text)):
        return ResponseContract()
    if re.search(r'\b(?:place|buy|purchase|cancel|approve|reject|apply|redeem|register|'
                 r'update|change|delete|remove|add|ship|deliver|create|quote|checkout)\b|'
                 r'\b(?:refund|return|exchange) (?:me|my|this|that|order|the item)\b|\b(?:download|pdf)\b', text):
        return ResponseContract()
    if mode != 'admin' and re.search(r'\b(?:analytics|coupons?|promo codes?|promotion codes?)\b', text):
        return ResponseContract()
    if not customer_id and mode == 'customer' and re.search(r'\bmy (?:orders?|invoices?|refunds?|account|credit|draft|spending|billing|purchases?)\b', text):
        return ResponseContract()
    if mode == 'admin':
        report = _chart_report(message, analytics_context)
        if report is not None:
            return report
    broad_analytics = (re.search(r'\b(?:analytics|dashboard|performance overview|business overview|store overview|(?:store|shop|business) (?:wide )?(?:metrics|statistics|summary))\b', text)
        or re.search(r'\bhow (?:is|s) (?:our|the|my) (?:store|shop|business) (?:doing|performing)\b', text))
    if mode == 'admin' and broad_analytics:
        focused = re.search(r'\b(?:only|just|top|by|trend|chart|compare|comparison|anomalies)\b', text)
        if not _named_chart_style(text) and (not focused or re.search(r'\b(?:comprehensive|everything|complete dashboard)\b', text)):
            filters, unresolved = _report_filters(message)
            if not unresolved:
                return ResponseContract('store_overview', bool(filters), frozenset({'finance'}), filters)
            return ResponseContract('data', domains=_domains(text) | {'finance'})
    if mode == 'admin' and (chart_intent(message) or _named_chart_style(text)) and isinstance(analytics_context, dict):
        return ResponseContract('data')
    product = re.search(r'\b(?:products?|items?|categories|category|range)\b', text)
    broad = (re.search(r'\b(?:what|which) (?:all |are (?:all |the )?)?(?:products?|items?|categories)\b', text)
             or re.search(r'\b(?:show|list|display|give|tell)\b.*\b(?:products?|items?|categories|product range)\b', text))
    focused = re.search(r'\b(?:under|below|above|over|between|cheaper|best|recommend|compare|'
                        r'comparison|alternatives?|prices?|features?|specifications?|sku|item code|'
                        r'first|next|page|per page|limit|brand|named|called|in stock|chart|plot)\b|\d', text)
    remainder = re.sub(r'\b(?:please|can|could|would|you|u|me|us|show|list|display|give|tell|'
        r'what|which|all|are|the|do|does|have|has|sell|selling|stock|store|shop|in|at|your|our|'
        r'products?|items?|categories|category|and|number|no|of|counts?|available|availability|range|there|'
        r'now|currently|to|a|table|tabular|form|as|by|with|each|per|their|how|many|overview|summary)\b', '', text).strip(' ?')
    if product and broad and not focused and not remainder:
        # An explicit full item list uses normal detail pagination. A category
        # summary remains the default for availability/range questions.
        if re.search(r'\blist (?:all |the )?(?:products|items)\b', text) and not re.search(r'\b(?:categor(?:y|ies)|counts?|number|overview)\b', text):
            return ResponseContract('data', domains=frozenset({'products'}))
        return ResponseContract('inventory_overview', domains=frozenset({'products'}))
    domains = _domains(text)
    if records:
        return ResponseContract('data',domains=domains)
    direct = re.search(r'\b(?:show|list|display|find|search|fetch|check|get|compare|recommend|suggest)\b', text)
    discovery = re.search(r'\b(?:do|does) (?:you|u|the store) (?:have|sell|stock)\b', text)
    retrieval = direct or discovery or re.search(r'\b(?:how many|how much|what|which|is|are|when|where|status|available|availability)\b', text)
    if retrieval and (domains or direct or discovery):
        return ResponseContract('data', domains=domains)
    return ResponseContract()


def sql_text(query):
    """Remove data literals/comments before reading referenced SQL table names."""
    return re.sub(r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'", ' ', str(query)).casefold()


def _inventory_recipe(query):
    # Prove a small family of equivalent one-row-per-item queries. Arbitrary
    # joins or CTEs cannot establish complete inventory from aliases alone.
    tokens = re.findall(r"'(?:''|[^'])*'|--[^\n]*|/\*[\s\S]*?\*/|[^']+?", str(query))
    clean = ''.join(token for token in tokens if not token.startswith(('--', '/*')))
    clean = re.sub(r'\s+', '', clean).casefold().rstrip(';')
    clean = re.sub(r'["`\[\]]', '', clean)
    expression = r"coalesce\((?:nullif\((?:trim\(d\.category\)|d\.category),''\)|d\.category),'uncategorised'\)"
    count = r'count\((?:\*|distincti\.item_code)\)'
    pattern = (rf'select{expression}ascategory,{count}asproducts,'
        r'sum\(casewheni\.availablequantity>0then1else0end\)asin_stock_products'
        r'fromitems(?:as)?ileft(?:outer)?joinproduct_details(?:as)?dond\.item_code=i\.item_code'
        rf'groupby(?:category|{expression})orderbycategory(?:asc)?')
    return re.fullmatch(pattern, clean) is not None


def inventory_rows(arguments, data):
    if not data.get('ok') or data.get('truncated') or not _inventory_recipe(arguments.get('query', '')):
        return None
    if data.get('columns') != ['category', 'products', 'in_stock_products'] or not isinstance(data.get('rows'), list):
        return None
    result, seen = [], set()
    for row in data['rows']:
        if not isinstance(row, list) or len(row) != 3:
            return None
        category, count, stock = row
        if not isinstance(category, str) or not category.strip() or category in seen:
            return None
        if type(count) is not int or type(stock) is not int or not 0 <= stock <= count:
            return None
        seen.add(category)
        result.append([category, count, stock])
    return sorted(result, key=lambda row: row[0].casefold())


def _receipt_domains(name, arguments, data):
    if not data.get('ok'):
        return frozenset()
    if name == 'billing_analysis':
        report = data.get('report')
        if report == 'store_overview':
            return frozenset({'finance', 'orders', 'invoices', 'refunds', 'refund_requests', 'customers', 'products'})
        if report in ('pending_refunds', 'pending_above_threshold'):
            return frozenset({'orders', 'refunds', 'refund_requests'})
        if report == 'refund_reconciliation':
            return frozenset({'refunds'})
        return frozenset({'finance', 'orders', 'invoices', 'refunds'})
    if name == 'billing_anomalies':
        return frozenset({'anomalies'})
    if name in ('compare_products', 'find_cheaper_alternatives'):
        return frozenset({'products'})
    if name != 'execute_sql_query':
        return frozenset()
    query = sql_text(arguments.get('query', ''))
    if re.match(r'\s*pragma\b', query):
        return frozenset()
    tables = set(re.findall(r'\b(?:from|join)\s+["`\[]?(?:main\.)?([a-z_][a-z_0-9]*)', query))
    owners = {
        'products': {'items','product_details','product_attributes','product_relations','price_history'},
        'orders': {'orders','order_details','order_service','order_requests','order_fulfilled'},
        'invoices': {'orders','invoice_lines','invoice_revisions'},
        'refunds': {'refund_requests','refund_details'},
        'refund_requests': {'refund_requests'},
        'finance': {'orders','invoice_lines'},
        'customers': {'customers','customer_billing_details'},
        'credit': {'store_credit_ledger'}, 'anomalies': {'billing_audit'},
    }
    result = {domain for domain, sources in owners.items() if sources & tables}
    refund_fields = {'refund_amount', 'refund_status', 'refund_reason'}
    columns = data.get('columns', [])
    if 'orders' in tables and (re.search(r'\brefund_(?:amount|status|reason)\b', query) or
            isinstance(columns, list) and any(isinstance(column, str) and column.casefold() in refund_fields for column in columns)):
        result.add('refunds')
    business_tables = set().union(*owners.values(), {'billing_config', 'promotions'})
    if business_tables & tables:
        result.add('records')
    return frozenset(result)


def evidence_for(contract, name, arguments, data):
    if not data.get('ok'):
        return False
    if contract.kind == 'inventory_overview':
        return name == 'execute_sql_query' and inventory_rows(arguments, data) is not None
    if contract.kind == 'store_overview':
        actual = data.get('filters', {})
        return (name == 'billing_analysis' and data.get('report') == 'store_overview'
                and data.get('coverage', {}).get('complete') is True and isinstance(actual, dict)
                and {key: actual.get(key) for key in FILTER_KEYS} == contract.expected_filters
                and not any(value for key, value in actual.items() if key not in FILTER_KEYS)
                and data.get('chart_mode', 'overview') == contract.chart_mode)
    observed = _receipt_domains(name, arguments, data)
    return bool(observed & contract.domains) if contract.domains else bool(observed)


def evidence_satisfied(contract, receipts):
    if contract.kind != 'data':
        return any(evidence_for(contract, *receipt) for receipt in receipts)
    observed = frozenset().union(*(_receipt_domains(*receipt) for receipt in receipts))
    return contract.domains <= observed if contract.domains else bool(observed)


def report_reply(contract, receipt):
    """Stable presentation of verified reports; never reuse model-made numbers."""
    name, arguments, data = receipt
    if contract.kind == 'inventory_overview':
        rows = inventory_rows(arguments, data)
        total, available = sum(row[1] for row in rows), sum(row[2] for row in rows)
        if not rows:
            return 'There are no products in the store. The category table is empty.'
        return (f'The store has {total} product' + ('s' if total != 1 else '')
                + f' across {len(rows)} categor' + ('ies' if len(rows) != 1 else 'y') + '. '
                + f'{available} product' + ('s are' if available != 1 else ' is') + ' currently in stock. '
                'The table lists every category. Product counts refer to distinct products, not stock units.')
    summary = data['summary']
    reply = (f"The report covers {summary['matched_orders']} orders: {summary['invoice_count']} active issued invoices, "
            f"{summary['draft_count']} drafts and {summary['cancelled_count']} cancelled orders. "
            'It includes invoice totals, monthly sales, product and category sales, '
            'customer summaries, inventory and refund activity. Monetary totals are separated by currency. '
            'The tables show current results; complete datasets are available with the report. '
            'Product revenue is before order-level discounts, tax, shipping and refunds. '
            'Inventory is a current full-store snapshot.').strip()
    if contract.chart_mode in ('dashboard', 'all'):
        delivery = data.get('chart_delivery', {})
        count = delivery.get('attached_this_call', 0)
        reply += (f' {count} charts are attached.' if count else ' No charts could be generated from the matching data.')
        omissions = data.get('coverage', {}).get('chart_omissions', [])
        reasons = list(dict.fromkeys(f"{item.get('type', 'view')}: {item['reason']}" for item in omissions
                                    if isinstance(item, dict) and item.get('reason')))
        errors = delivery.get('errors', [])
        if reasons or errors:
            reply += ' Unavailable views: ' + '; '.join(dict.fromkeys([*reasons, *errors])) + '.'
    return reply
