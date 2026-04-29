"""
gui/app.py — Judgeman GUI backend (Flask REST API)
Every route maps directly to the existing judgeman engine — no logic duplication.
"""
import sys, os, json, uuid, traceback
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory

JUDGEMAN_DIR = Path(__file__).parent.parent / 'judgeman'
sys.path.insert(0, str(JUDGEMAN_DIR))
sys.path.insert(0, str(JUDGEMAN_DIR / 'commands'))

import db, audit as audit_mod
from compat import get_analyst_id, get_home_dir
from models import utc_now, IMPACT_CEILINGS
from confidence import calculate_confidence, AUTOLIFT_CRITERIA
from chainhash import compute_audit_chain_hash

app = Flask(__name__, template_folder='templates', static_folder='static')

def get_conn():
    db.init_db()
    return db.get_connection()

def err(msg, code=400):
    return jsonify({'error': msg}), code

def ok(data=None, msg=None):
    r = {'ok': True}
    if data is not None: r['data'] = data
    if msg: r['message'] = msg
    return jsonify(r)

def bd_to_dict(bd):
    return {
        'claim_id': bd.claim_id,
        'impact_level': bd.impact_level,
        'base_confidence': bd.base_confidence,
        'raw_confidence': bd.raw_confidence,
        'ceiling': bd.ceiling,
        'ceiling_applied': bd.ceiling_applied,
        'final_confidence': bd.final_confidence,
        'displayed_confidence': bd.displayed_confidence(),
        'override_active': bd.override_active,
        'override_confidence': bd.override_confidence,
        'override_justification': bd.override_justification,
        'has_counter_claim': bd.has_counter_claim,
        'has_what_if_wrong': bd.has_what_if_wrong,
        'high_impact_requirements_met': bd.high_impact_requirements_met,
        'factors': [{'name': f.name, 'value': round(f.value,4), 'explanation': f.explanation} for f in bd.factors],
        'improvement_paths': bd.improvement_paths,
        'reduction_risks': bd.reduction_risks,
        'autolift': {
            'applicable': bd.autolift.applicable,
            'criteria_met': bd.autolift.criteria_met,
            'lifted_ceiling': bd.autolift.lifted_ceiling,
            'met_criteria': bd.autolift.met_criteria(),
            'unmet_criteria': bd.autolift.unmet_criteria(),
            'high_cred_groups_found': bd.autolift.high_cred_groups_found,
            'independent_groups_required': bd.autolift.independent_groups_required,
            'addressed_counter_claims': bd.autolift.addressed_counter_claims,
        } if bd.autolift else None,
        'summary': bd.summary_line(),
    }

@app.route('/')
def index():
    return send_from_directory(app.template_folder, 'index.html')

# ── Investigations ──────────────────────────────────────────────
@app.route('/api/investigations', methods=['GET'])
def list_investigations():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM investigations ORDER BY created_at DESC").fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/api/investigations', methods=['POST'])
def create_investigation():
    d = request.json or {}
    name = (d.get('name') or '').strip()
    analyst = get_analyst_id(d.get('analyst'))
    description = (d.get('description') or '').strip() or None
    if not name: return err('Name required')
    conn = get_conn()
    inv_id = str(uuid.uuid4())
    now = utc_now()
    with conn:
        conn.execute("INSERT INTO investigations (id,name,description,status,analyst_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (inv_id, name, description, 'active', analyst, now, now))
    db.set_active_investigation(conn, inv_id, analyst)
    audit_mod.log_action(conn, analyst, audit_mod.CREATE_INVESTIGATION, 'investigation',
        entity_id=inv_id, investigation_id=inv_id, new_value={'name': name})
    conn.close()
    return ok({'id': inv_id, 'name': name}, 'Investigation created')

@app.route('/api/investigations/active', methods=['GET'])
def get_active():
    conn = get_conn()
    inv = db.get_active_investigation(conn)
    conn.close()
    return jsonify(inv)

@app.route('/api/investigations/<inv_id>/activate', methods=['POST'])
def activate(inv_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM investigations WHERE id=?", (inv_id,)).fetchone()
    if not row: conn.close(); return err('Not found', 404)
    analyst = get_analyst_id((request.json or {}).get('analyst'))
    db.set_active_investigation(conn, inv_id, analyst)
    audit_mod.log_action(conn, analyst, audit_mod.SET_ACTIVE, 'investigation', entity_id=inv_id, investigation_id=inv_id)
    conn.close()
    return ok(msg=f"Active set to {row['name']}")

@app.route('/api/investigations/<inv_id>/close', methods=['POST'])
def close_inv(inv_id):
    d = request.json or {}
    stmt = (d.get('statement') or '').strip()
    if not stmt: return err('Closing statement required')
    prohibited = ['is guilty','is liable','should be arrested','is a criminal','proved that','recommends prosecution']
    found = [p for p in prohibited if p.lower() in stmt.lower()]
    if found and not d.get('force'): return jsonify({'error': f'Prohibited language detected: {found}', 'prohibited': True}), 422
    conn = get_conn()
    row = conn.execute("SELECT * FROM investigations WHERE id=?", (inv_id,)).fetchone()
    if not row: conn.close(); return err('Not found', 404)
    with conn:
        conn.execute("UPDATE investigations SET status='closed',updated_at=? WHERE id=?", (utc_now(), inv_id))
    audit_mod.log_action(conn, row['analyst_id'], audit_mod.CLOSE_INVESTIGATION, 'investigation',
        entity_id=inv_id, investigation_id=inv_id,
        old_value={'status':'active'}, new_value={'status':'closed','closing_statement':stmt}, justification=stmt)
    conn.close()
    return ok(msg='Investigation closed')

@app.route('/api/investigations/<inv_id>/reopen', methods=['POST'])
def reopen_inv(inv_id):
    d = request.json or {}
    reason = (d.get('reason') or '').strip()
    if not reason: return err('Reason required')
    conn = get_conn()
    row = conn.execute("SELECT * FROM investigations WHERE id=?", (inv_id,)).fetchone()
    if not row: conn.close(); return err('Not found', 404)
    with conn:
        conn.execute("UPDATE investigations SET status='active',updated_at=? WHERE id=?", (utc_now(), inv_id))
    audit_mod.log_action(conn, row['analyst_id'], 'REOPEN_INVESTIGATION', 'investigation',
        entity_id=inv_id, investigation_id=inv_id, new_value={'status':'active'}, justification=reason)
    conn.close()
    return ok(msg='Reopened')

@app.route('/api/investigation/summary', methods=['GET'])
def summary():
    conn = get_conn()
    inv = db.get_active_investigation(conn)
    if not inv: conn.close(); return jsonify(None)
    iid = inv['id']
    hypotheses = [dict(r) for r in conn.execute("SELECT * FROM hypotheses WHERE investigation_id=? ORDER BY created_at",(iid,)).fetchall()]
    sources = [dict(r) for r in conn.execute("SELECT s.*,COUNT(e.id) as ev_count FROM sources s LEFT JOIN evidence e ON e.source_id=s.id WHERE s.investigation_id=? GROUP BY s.id ORDER BY s.credibility_score DESC",(iid,)).fetchall()]
    evidence = [dict(r) for r in conn.execute("SELECT e.*,s.name as source_name,s.credibility_score FROM evidence e JOIN sources s ON s.id=e.source_id WHERE e.investigation_id=? ORDER BY e.created_at DESC",(iid,)).fetchall()]
    claims_raw = [dict(r) for r in conn.execute("SELECT c.*,COUNT(DISTINCT ec.evidence_id) as ev_count,COUNT(DISTINCT cc.id) as cc_count,SUM(CASE WHEN cc.addressed=0 THEN 1 ELSE 0 END) as open_cc FROM claims c LEFT JOIN evidence_claims ec ON ec.claim_id=c.id LEFT JOIN counter_claims cc ON cc.claim_id=c.id WHERE c.investigation_id=? GROUP BY c.id ORDER BY c.impact_level DESC,c.created_at",(iid,)).fetchall()]
    claims = []
    for c in claims_raw:
        try:
            bd = calculate_confidence(c['id'], conn)
            c['confidence'] = bd_to_dict(bd)
        except Exception: c['confidence'] = None
        claims.append(c)
    ev_claims = [dict(r) for r in conn.execute("SELECT ec.* FROM evidence_claims ec JOIN claims c ON c.id=ec.claim_id WHERE c.investigation_id=?",(iid,)).fetchall()]
    counter_claims = [dict(r) for r in conn.execute("SELECT cc.* FROM counter_claims cc JOIN claims c ON c.id=cc.claim_id WHERE c.investigation_id=? ORDER BY cc.created_at",(iid,)).fetchall()]
    conn.close()
    return jsonify({'investigation':inv,'hypotheses':hypotheses,'sources':sources,'evidence':evidence,'claims':claims,'evidence_claims':ev_claims,'counter_claims':counter_claims})

# ── Hypotheses ──────────────────────────────────────────────────
@app.route('/api/hypotheses', methods=['POST'])
def add_hypothesis():
    d = request.json or {}
    conn = get_conn()
    inv = db.get_active_investigation(conn)
    if not inv: conn.close(); return err('No active investigation')
    stmt = (d.get('statement') or '').strip()
    rat = (d.get('rationale') or '').strip() or None
    if not stmt: conn.close(); return err('Statement required')
    hid = str(uuid.uuid4()); now = utc_now()
    with conn:
        conn.execute("INSERT INTO hypotheses (id,investigation_id,statement,status,rationale,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",(hid,inv['id'],stmt,'active',rat,now,now))
    audit_mod.log_action(conn,inv['analyst_id'],audit_mod.CREATE_HYPOTHESIS,'hypothesis',entity_id=hid,investigation_id=inv['id'],new_value={'statement':stmt})
    conn.close()
    return ok({'id':hid},'Hypothesis added')

@app.route('/api/hypotheses/<hid>', methods=['PATCH'])
def update_hypothesis(hid):
    d = request.json or {}
    conn = get_conn()
    inv = db.get_active_investigation(conn)
    if not inv: conn.close(); return err('No active investigation')
    status = d.get('status'); rat = (d.get('rationale') or '').strip()
    if status not in ['active','supported','rejected','inconclusive']: conn.close(); return err('Invalid status')
    if not rat: conn.close(); return err('Rationale required')
    row = conn.execute("SELECT * FROM hypotheses WHERE id=? AND investigation_id=?",(hid,inv['id'])).fetchone()
    if not row: conn.close(); return err('Not found',404)
    with conn:
        conn.execute("UPDATE hypotheses SET status=?,rationale=?,updated_at=? WHERE id=?",(status,rat,utc_now(),hid))
    audit_mod.log_action(conn,inv['analyst_id'],audit_mod.UPDATE_HYPOTHESIS,'hypothesis',entity_id=hid,investigation_id=inv['id'],old_value={'status':row['status']},new_value={'status':status},justification=rat)
    conn.close()
    return ok(msg=f'Status → {status}')

# ── Sources ─────────────────────────────────────────────────────
@app.route('/api/sources', methods=['POST'])
def add_source():
    d = request.json or {}
    conn = get_conn()
    inv = db.get_active_investigation(conn)
    if not inv: conn.close(); return err('No active investigation')
    name=(d.get('name') or '').strip(); ref=(d.get('reference') or '').strip()
    stype=d.get('source_type','documentary'); cred_rat=(d.get('credibility_rationale') or '').strip()
    grp=(d.get('independence_group') or '').strip() or None
    try: cred=float(d.get('credibility_score')); assert 0<=cred<=1
    except Exception: conn.close(); return err('Credibility must be 0.0–1.0')
    if not name or not ref or not cred_rat: conn.close(); return err('Name, reference, and credibility rationale required')
    if stype not in ['primary','secondary','tertiary','human','technical','documentary']: conn.close(); return err('Invalid type')
    sid=str(uuid.uuid4()); now=utc_now()
    with conn:
        conn.execute("INSERT INTO sources (id,investigation_id,name,reference,source_type,credibility_score,credibility_rationale,independence_group,created_at) VALUES (?,?,?,?,?,?,?,?,?)",(sid,inv['id'],name,ref,stype,cred,cred_rat,grp,now))
    audit_mod.log_action(conn,inv['analyst_id'],audit_mod.CREATE_SOURCE,'source',entity_id=sid,investigation_id=inv['id'],new_value={'name':name,'credibility_score':cred})
    conn.close()
    return ok({'id':sid},'Source added')

@app.route('/api/sources/<sid>', methods=['PATCH'])
def update_source(sid):
    d = request.json or {}
    conn = get_conn()
    inv = db.get_active_investigation(conn)
    if not inv: conn.close(); return err('No active investigation')
    row=conn.execute("SELECT * FROM sources WHERE id=? AND investigation_id=?",(sid,inv['id'])).fetchone()
    if not row: conn.close(); return err('Not found',404)
    try: cred=float(d.get('credibility_score')); assert 0<=cred<=1
    except Exception: conn.close(); return err('Credibility must be 0.0–1.0')
    rat=(d.get('credibility_rationale') or '').strip()
    if not rat: conn.close(); return err('Rationale required')
    with conn:
        conn.execute("UPDATE sources SET credibility_score=?,credibility_rationale=? WHERE id=?",(cred,rat,sid))
    audit_mod.log_action(conn,inv['analyst_id'],'UPDATE_SOURCE_CREDIBILITY','source',entity_id=sid,investigation_id=inv['id'],old_value={'credibility_score':row['credibility_score']},new_value={'credibility_score':cred},justification=rat)
    conn.close()
    return ok(msg='Credibility updated')

# ── Evidence ────────────────────────────────────────────────────
@app.route('/api/evidence', methods=['POST'])
def add_evidence():
    d = request.json or {}
    conn = get_conn()
    inv = db.get_active_investigation(conn)
    if not inv: conn.close(); return err('No active investigation')
    desc=(d.get('description') or '').strip(); src_id=(d.get('source_id') or '').strip()
    ref=(d.get('raw_content_ref') or '').strip() or None
    if not desc or not src_id: conn.close(); return err('Description and source required')
    src=conn.execute("SELECT id FROM sources WHERE id=? AND investigation_id=?",(src_id,inv['id'])).fetchone()
    if not src: conn.close(); return err('Source not found',404)
    eid=str(uuid.uuid4()); now=utc_now()
    with conn:
        conn.execute("INSERT INTO evidence (id,investigation_id,source_id,description,raw_content_ref,collected_at,created_at) VALUES (?,?,?,?,?,?,?)",(eid,inv['id'],src_id,desc,ref,now,now))
    audit_mod.log_action(conn,inv['analyst_id'],audit_mod.CREATE_EVIDENCE,'evidence',entity_id=eid,investigation_id=inv['id'],new_value={'source_id':src_id,'description':desc})
    conn.close()
    return ok({'id':eid},'Evidence recorded')

# ── Claims ──────────────────────────────────────────────────────
@app.route('/api/claims', methods=['POST'])
def create_claim():
    d = request.json or {}
    conn = get_conn()
    inv = db.get_active_investigation(conn)
    if not inv: conn.close(); return err('No active investigation')
    stmt=(d.get('statement') or '').strip(); rat=(d.get('rationale') or '').strip()
    impact=d.get('impact_level','low'); wiw=(d.get('what_if_wrong') or '').strip() or None
    hyp_id=(d.get('hypothesis_id') or '').strip() or None
    try: base=float(d.get('base_confidence')); assert 0<=base<=1
    except Exception: conn.close(); return err('Base confidence must be 0.0–1.0')
    if not stmt or not rat: conn.close(); return err('Statement and rationale required')
    if impact not in ['low','medium','high']: conn.close(); return err('Invalid impact level')
    if impact=='high' and not wiw: conn.close(); return err("High-impact claims require 'what_if_wrong'")
    if hyp_id:
        hyp=conn.execute("SELECT id FROM hypotheses WHERE id=? AND investigation_id=?",(hyp_id,inv['id'])).fetchone()
        if not hyp: hyp_id=None
    cid=str(uuid.uuid4()); now=utc_now()
    try:
        with conn:
            conn.execute("INSERT INTO claims (id,investigation_id,hypothesis_id,statement,base_confidence,rationale,what_if_wrong,impact_level,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",(cid,inv['id'],hyp_id,stmt,base,rat,wiw,impact,now,now))
    except Exception as e: conn.close(); return err(str(e))
    audit_mod.log_action(conn,inv['analyst_id'],audit_mod.CREATE_CLAIM,'claim',entity_id=cid,investigation_id=inv['id'],new_value={'statement':stmt,'base_confidence':base,'impact_level':impact})
    conn.close()
    return ok({'id':cid},'Claim created')

@app.route('/api/claims/<cid>', methods=['GET'])
def get_claim(cid):
    conn = get_conn()
    inv = db.get_active_investigation(conn)
    if not inv: conn.close(); return err('No active investigation')
    row=conn.execute("SELECT * FROM claims WHERE id=? AND investigation_id=?",(cid,inv['id'])).fetchone()
    if not row: conn.close(); return err('Not found',404)
    claim=dict(row)
    bd=calculate_confidence(cid,conn)
    claim['confidence']=bd_to_dict(bd)
    ev_links=[dict(r) for r in conn.execute("SELECT ec.*,e.description,s.name as source_name,s.credibility_score FROM evidence_claims ec JOIN evidence e ON e.id=ec.evidence_id JOIN sources s ON s.id=e.source_id WHERE ec.claim_id=?",(cid,)).fetchall()]
    ccs=[dict(r) for r in conn.execute("SELECT * FROM counter_claims WHERE claim_id=? ORDER BY created_at",(cid,)).fetchall()]
    conn.close()
    claim['evidence_links']=ev_links; claim['counter_claims']=ccs
    return jsonify(claim)

@app.route('/api/claims/<cid>/link', methods=['POST'])
def link_ev(cid):
    d=request.json or {}
    conn=get_conn(); inv=db.get_active_investigation(conn)
    if not inv: conn.close(); return err('No active investigation')
    ev_id=(d.get('evidence_id') or '').strip(); rel=d.get('relationship','supports')
    note=(d.get('relevance_note') or '').strip() or None
    if rel not in ['supports','undermines','neutral']: conn.close(); return err('Invalid relationship')
    if not conn.execute("SELECT id FROM claims WHERE id=? AND investigation_id=?",(cid,inv['id'])).fetchone(): conn.close(); return err('Claim not found',404)
    if not conn.execute("SELECT id FROM evidence WHERE id=? AND investigation_id=?",(ev_id,inv['id'])).fetchone(): conn.close(); return err('Evidence not found',404)
    now=utc_now()
    existing=conn.execute("SELECT * FROM evidence_claims WHERE evidence_id=? AND claim_id=?",(ev_id,cid)).fetchone()
    with conn:
        if existing: conn.execute("UPDATE evidence_claims SET relationship=?,relevance_note=?,linked_at=? WHERE evidence_id=? AND claim_id=?",(rel,note,now,ev_id,cid))
        else: conn.execute("INSERT INTO evidence_claims (evidence_id,claim_id,relationship,relevance_note,linked_at) VALUES (?,?,?,?,?)",(ev_id,cid,rel,note,now))
    audit_mod.log_action(conn,inv['analyst_id'],audit_mod.LINK_EVIDENCE,'evidence_claim',entity_id=ev_id,investigation_id=inv['id'],new_value={'claim_id':cid,'relationship':rel})
    bd=calculate_confidence(cid,conn)
    with conn: conn.execute("UPDATE claims SET final_confidence=?,updated_at=? WHERE id=?",(bd.final_confidence,now,cid))
    conn.close()
    return ok({'confidence':bd_to_dict(bd)},f'Linked as {rel}')

@app.route('/api/claims/<cid>/link/<ev_id>', methods=['DELETE'])
def unlink_ev(cid,ev_id):
    d=request.json or {}; reason=(d.get('reason') or '').strip()
    if not reason: return err('Reason required')
    conn=get_conn(); inv=db.get_active_investigation(conn)
    if not inv: conn.close(); return err('No active investigation')
    link=conn.execute("SELECT * FROM evidence_claims WHERE evidence_id=? AND claim_id=?",(ev_id,cid)).fetchone()
    if not link: conn.close(); return err('Link not found',404)
    with conn: conn.execute("DELETE FROM evidence_claims WHERE evidence_id=? AND claim_id=?",(ev_id,cid))
    audit_mod.log_action(conn,inv['analyst_id'],'UNLINK_EVIDENCE','evidence_claim',entity_id=ev_id,investigation_id=inv['id'],old_value={'claim_id':cid,'relationship':link['relationship']},justification=reason)
    conn.close(); return ok(msg='Unlinked')

@app.route('/api/claims/<cid>/challenge', methods=['POST'])
def challenge(cid):
    d=request.json or {}
    conn=get_conn(); inv=db.get_active_investigation(conn)
    if not inv: conn.close(); return err('No active investigation')
    stmt=(d.get('statement') or '').strip()
    if not stmt: conn.close(); return err('Statement required')
    if not conn.execute("SELECT id FROM claims WHERE id=? AND investigation_id=?",(cid,inv['id'])).fetchone(): conn.close(); return err('Claim not found',404)
    ccid=str(uuid.uuid4()); now=utc_now()
    with conn: conn.execute("INSERT INTO counter_claims (id,claim_id,statement,addressed,created_at,updated_at) VALUES (?,?,?,0,?,?)",(ccid,cid,stmt,now,now))
    audit_mod.log_action(conn,inv['analyst_id'],audit_mod.ADD_COUNTER_CLAIM,'counter_claim',entity_id=ccid,investigation_id=inv['id'],new_value={'claim_id':cid,'statement':stmt})
    bd=calculate_confidence(cid,conn)
    with conn: conn.execute("UPDATE claims SET final_confidence=?,updated_at=? WHERE id=?",(bd.final_confidence,now,cid))
    conn.close(); return ok({'id':ccid,'confidence':bd_to_dict(bd)},'Counter-claim added')

@app.route('/api/counterclaims/<ccid>/address', methods=['POST'])
def address_cc(ccid):
    d=request.json or {}; rat=(d.get('rationale') or '').strip()
    if not rat: return err('Rationale required')
    if len(rat)<20: return err('Rationale must be substantive (>20 chars)')
    conn=get_conn(); inv=db.get_active_investigation(conn)
    if not inv: conn.close(); return err('No active investigation')
    cc=conn.execute("SELECT cc.*,c.investigation_id,c.id as cid FROM counter_claims cc JOIN claims c ON cc.claim_id=c.id WHERE cc.id=?",(ccid,)).fetchone()
    if not cc or cc['investigation_id']!=inv['id']: conn.close(); return err('Not found',404)
    if cc['addressed']: conn.close(); return err('Already addressed')
    now=utc_now()
    with conn: conn.execute("UPDATE counter_claims SET addressed=1,address_rationale=?,updated_at=? WHERE id=?",(rat,now,ccid))
    audit_mod.log_action(conn,inv['analyst_id'],audit_mod.ADDRESS_COUNTER_CLAIM,'counter_claim',entity_id=ccid,investigation_id=inv['id'],old_value={'addressed':False},new_value={'addressed':True,'rationale':rat},justification=rat)
    bd=calculate_confidence(cc['cid'],conn)
    with conn: conn.execute("UPDATE claims SET final_confidence=?,updated_at=? WHERE id=?",(bd.final_confidence,now,cc['cid']))
    conn.close(); return ok({'confidence':bd_to_dict(bd)},'Counter-claim addressed')

@app.route('/api/claims/<cid>/override', methods=['POST'])
def override_conf(cid):
    d=request.json or {}
    conn=get_conn(); inv=db.get_active_investigation(conn)
    if not inv: conn.close(); return err('No active investigation')
    row=conn.execute("SELECT * FROM claims WHERE id=? AND investigation_id=?",(cid,inv['id'])).fetchone()
    if not row: conn.close(); return err('Not found',404)
    try: proposed=float(d.get('confidence')); assert 0<=proposed<=1
    except Exception: conn.close(); return err('Confidence must be 0.0–1.0')
    just=(d.get('justification') or '').strip()
    if not just or len(just)<30: conn.close(); return err('Justification required (>30 chars)')
    now=utc_now()
    with conn: conn.execute("UPDATE claims SET override_confidence=?,override_justification=?,updated_at=? WHERE id=?",(proposed,just,now,cid))
    audit_mod.log_action(conn,inv['analyst_id'],audit_mod.OVERRIDE_CEILING,'claim',entity_id=cid,investigation_id=inv['id'],old_value={'override_confidence':row['override_confidence']},new_value={'override_confidence':proposed},justification=just)
    conn.close(); return ok({'override_confidence':proposed},'Override recorded')

@app.route('/api/claims/<cid>/edit', methods=['PATCH'])
def edit_claim(cid):
    d=request.json or {}
    conn=get_conn(); inv=db.get_active_investigation(conn)
    if not inv: conn.close(); return err('No active investigation')
    row=conn.execute("SELECT * FROM claims WHERE id=? AND investigation_id=?",(cid,inv['id'])).fetchone()
    if not row: conn.close(); return err('Not found',404)
    stmt=(d.get('statement') or row['statement']).strip()
    rat=(d.get('rationale') or row['rationale']).strip()
    wiw=(d.get('what_if_wrong') or row['what_if_wrong'] or '').strip() or None
    if row['impact_level']=='high' and not wiw: conn.close(); return err("High-impact requires what_if_wrong")
    now=utc_now()
    try:
        with conn: conn.execute("UPDATE claims SET statement=?,rationale=?,what_if_wrong=?,updated_at=? WHERE id=?",(stmt,rat,wiw,now,cid))
    except Exception as e: conn.close(); return err(str(e))
    audit_mod.log_action(conn,inv['analyst_id'],'EDIT_CLAIM','claim',entity_id=cid,investigation_id=inv['id'],old_value={'statement':row['statement']},new_value={'statement':stmt})
    conn.close(); return ok(msg='Claim updated')

# ── Audit ────────────────────────────────────────────────────────
@app.route('/api/audit', methods=['GET'])
def get_audit():
    conn=get_conn(); inv=db.get_active_investigation(conn)
    if not inv: conn.close(); return jsonify([])
    limit=int(request.args.get('limit',100))
    rows=[dict(r) for r in conn.execute("SELECT * FROM analyst_actions WHERE investigation_id=? ORDER BY timestamp DESC LIMIT ?",(inv['id'],limit)).fetchall()]
    conn.close(); return jsonify(rows)

# ── Verify ───────────────────────────────────────────────────────
@app.route('/api/verify', methods=['GET'])
def verify():
    conn=get_conn(); inv=db.get_active_investigation(conn)
    if not inv: conn.close(); return jsonify({'blocking':[],'warnings':[],'infos':[],'passed':True})
    iid=inv['id']
    blocking,warnings,infos=[],[],[]
    claims=[dict(r) for r in conn.execute("SELECT * FROM claims WHERE investigation_id=?",(iid,)).fetchall()]
    hyps=[dict(r) for r in conn.execute("SELECT * FROM hypotheses WHERE investigation_id=?",(iid,)).fetchall()]
    sources=[dict(r) for r in conn.execute("SELECT s.*,COUNT(e.id) as ec FROM sources s LEFT JOIN evidence e ON e.source_id=s.id WHERE s.investigation_id=? GROUP BY s.id",(iid,)).fetchall()]
    evidence=[dict(r) for r in conn.execute("SELECT e.*,COUNT(ec.claim_id) as lc FROM evidence e LEFT JOIN evidence_claims ec ON ec.evidence_id=e.id WHERE e.investigation_id=? GROUP BY e.id",(iid,)).fetchall()]
    if not hyps: warnings.append('No hypotheses defined.')
    if not sources: warnings.append('No sources registered.')
    for c in claims:
        s=c['statement'][:50]; cid=c['id'][:8]
        if c['impact_level']=='high':
            cc_n=conn.execute("SELECT COUNT(*) FROM counter_claims WHERE claim_id=?",(c['id'],)).fetchone()[0]
            if cc_n==0: blocking.append(f"HIGH CLAIM {cid}… '{s}' — no counter-claim")
            if not c['what_if_wrong']: blocking.append(f"HIGH CLAIM {cid}… '{s}' — missing what_if_wrong")
        ev_n=conn.execute("SELECT COUNT(*) FROM evidence_claims WHERE claim_id=?",(c['id'],)).fetchone()[0]
        if ev_n==0: blocking.append(f"CLAIM {cid}… '{s}' — no linked evidence")
    for s in sources:
        if s['ec']==0: warnings.append(f"SOURCE '{s['name']}' has no evidence items.")
    for e in evidence:
        if e['lc']==0: warnings.append(f"EVIDENCE '{e['description'][:40]}' not linked to any claim.")
    susp=conn.execute("SELECT cc.statement,cc.address_rationale FROM counter_claims cc JOIN claims c ON cc.claim_id=c.id WHERE c.investigation_id=? AND cc.addressed=1 AND LENGTH(cc.address_rationale)<30",(iid,)).fetchall()
    for s in susp: warnings.append(f"Counter-claim '{s['statement'][:40]}' has very short address rationale.")
    if hyps:
        ul=conn.execute("SELECT COUNT(*) FROM claims WHERE investigation_id=? AND hypothesis_id IS NULL",(iid,)).fetchone()[0]
        if ul: infos.append(f"{ul} claim(s) not linked to any hypothesis.")
    conn.close()
    return jsonify({'blocking':blocking,'warnings':warnings,'infos':infos,'passed':len(blocking)==0})

# ── Report ───────────────────────────────────────────────────────
@app.route('/api/report', methods=['GET'])
def get_report():
    conn=get_conn(); inv=db.get_active_investigation(conn)
    if not inv: conn.close(); return err('No active investigation')
    try:
        from export_cmd import _build_report_lines
        lines=_build_report_lines(conn,inv)
        report='\n'.join(lines)
    except Exception as e:
        report=f"# Report generation error\n\n{traceback.format_exc()}"
    audit_mod.log_action(conn,inv['analyst_id'],audit_mod.GENERATE_REPORT,'investigation',entity_id=inv['id'],investigation_id=inv['id'])
    conn.close(); return jsonify({'markdown':report,'investigation_name':inv['name']})

# ── Export ───────────────────────────────────────────────────────
@app.route('/api/export', methods=['POST'])
def export_inv():
    d=request.json or {}
    note=(d.get('note') or '').strip() or None
    raw_dir = d.get('output_dir') or '~/Downloads'
    output_dir=Path(os.path.expanduser(raw_dir))
    output_dir.mkdir(parents=True, exist_ok=True)
    conn=get_conn(); inv=db.get_active_investigation(conn); conn.close()
    if not inv: return err('No active investigation')
    try:
        from click.testing import CliRunner
        from cli import cli
        runner=CliRunner()
        args=['export','--output-dir',str(output_dir)]
        if note: args+=['--note',note]
        result=runner.invoke(cli,args)
        import glob
        zips=sorted(glob.glob(str(output_dir/'judgeman_*.zip')))
        return ok({'path':zips[-1] if zips else None,'output':result.output})
    except Exception as e:
        return err(f'Export error: {e}')

if __name__=='__main__':
    import webbrowser, threading, time
    port=int(os.environ.get('JUDGEMAN_PORT',7432))
    def open_browser():
        time.sleep(1.0)
        webbrowser.open(f'http://127.0.0.1:{port}')
    threading.Thread(target=open_browser,daemon=True).start()
    print(f'\n  Judgeman is running at http://127.0.0.1:{port}')
    print('  Press Ctrl+C to quit\n')
    app.run(port=port,debug=False,threaded=True,host='127.0.0.1')
