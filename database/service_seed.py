"""Fictional service scenarios; called only by the explicit seed generator."""
from datetime import timedelta
import json
from decimal import Decimal
from mcp_server.tools.billing_tools import money, totals, update_totals


def seed_service_scenarios(conn,as_of,demo_order):
    customer='FAKE-CUST-0001'
    if not conn.execute('SELECT 1 FROM customers WHERE customer_id=?',(customer,)).fetchone(): return
    conn.execute("INSERT OR IGNORE INTO customer_billing_details VALUES (?, '100 Fictional Example Road, Demo City', 'DEMO-TAX-001')",(customer,))
    for label,qty in [('PARTIAL',1),('DUPLICATE',1),('B2G',3),('SHIPPED',1),('DELIVERED',1),('PENDING',10)]:
        oid='FAKE-SVC-'+label
        if conn.execute('SELECT 1 FROM orders WHERE order_id=?',(oid,)).fetchone(): continue
        item=conn.execute("SELECT item_code FROM items WHERE item_code GLOB 'FAKE-*' AND availablequantity>=? ORDER BY sellingprice DESC,item_code LIMIT 1",(qty,)).fetchone()
        if item is None: raise ValueError('Insufficient stock for service demo.')
        demo_order(conn,oid,customer,[(item[0],qty)],as_of,placed=True)
        conn.execute('UPDATE orders SET order_invoice=? WHERE order_id=?',('INV-'+oid,oid))
        currency=conn.execute("SELECT value FROM billing_config WHERE key='currency'").fetchone()[0]
        contact=dict(conn.execute('SELECT * FROM customers WHERE customer_id=?',(customer,)).fetchone())
        contact.update(billing_address='100 Fictional Example Road, Demo City',tax_registration_number='DEMO-TAX-001')
        conn.execute('INSERT OR IGNORE INTO order_details(order_id,currency,customer_snapshot) VALUES (?,?,?)',(oid,currency,json.dumps(contact)))
        state='shipped' if label in ('SHIPPED','PARTIAL') else 'delivered' if label=='DELIVERED' else 'placed'
        conn.execute('INSERT INTO order_service(order_id,fulfillment_status) VALUES (?,?)',(oid,state))
        if state in ('shipped','delivered'):
            conn.execute('UPDATE order_service SET shipped_at=? WHERE order_id=?',(as_of.isoformat(),oid))
        if state=='delivered': conn.execute('UPDATE order_service SET delivered_at=? WHERE order_id=?',(as_of.isoformat(),oid))
        if label=='PARTIAL':
            conn.execute('UPDATE order_requests SET quantity=2 WHERE order_id=?',(oid,))
            conn.execute("UPDATE order_service SET review_note='Fictional customer reports a partial charge. Only one of two requested units is fulfilled and invoiced; actual bank charges are unverified.' WHERE order_id=?",(oid,))
        if label=='DUPLICATE':
            conn.execute("UPDATE order_service SET review_note='Fictional customer reports two equal card charges. Only one local invoice exists; no payment evidence is stored. Requires staff review.',review_requested_at=CURRENT_TIMESTAMP WHERE order_id=?",(oid,))
        if label=='B2G':
            line=conn.execute('SELECT unit_price_billed FROM invoice_lines WHERE order_id=?',(oid,)).fetchone()
            code='FAKE-B2G1'
            conn.execute('INSERT OR IGNORE INTO promotions(promo_code,description,discount_type,discount_value,expires_on) VALUES (?,?,?,?,?)',(code,'Fictional buy two get one offer; paid value allocated across all three units','flat',str(money(line[0])),(as_of+timedelta(days=365)).isoformat()))
            subtotal=money(line[0])*3
            conn.execute('UPDATE orders SET promo_code=? WHERE order_id=?',(code,oid))
            update_totals(conn,oid,totals(conn,subtotal,code,check_expiry=False))
        if label=='PENDING':
            amount=conn.execute('SELECT charged_price FROM orders WHERE order_id=?',(oid,)).fetchone()[0]
            conn.execute("INSERT INTO refund_requests(order_id,amount,currency,reason) VALUES (?,?,?,'Fictional customer return awaiting admin review')",(oid,amount,currency))
            conn.execute("INSERT INTO refund_details(order_id,request_kind) VALUES (?,'return')",(oid,))
