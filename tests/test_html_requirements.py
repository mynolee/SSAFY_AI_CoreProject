"""Execute HTML handlers in Node against synthetic DOM and HTTP boundaries."""
from contextlib import ExitStack
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ids = new Map();
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.listeners = {}; this.value = ''; this.className = ''; this._text = ''; this.disabled = false; this.hidden = false; this.classList = { toggle() {} }; }
  set id(value) { this._id = value; ids.set(value, this); }
  get id() { return this._id; }
  set value(value) { this._value = String(value); }
  get value() { return this._value; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  replaceChildren(...nodes) { this._text = ''; this.children = []; this.append(...nodes); }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  fire(type) { for (const callback of this.listeners[type] || []) callback({ preventDefault() {} }); }
  after() {}
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
  querySelectorAll(selector) {
    const matches = node => selector === '.empty' ? node.className === 'empty' : selector === 'a' ? node.tagName === 'a' : selector.startsWith('button') ? node.tagName === 'button' : false;
    return this.children.flatMap(node => [...(matches(node) ? [node] : []), ...node.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
const html = fs.readFileSync('web/index.html', 'utf8');
for (const match of html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g)) { const node = new Element(match[1]); node.id = match[2]; }
const applicabilityInputs = [...html.matchAll(/data-applicability="([^"]+)"/g)].map(match => { const node = new Element('input'); node.dataset.applicability = match[1]; return node; });
for (const name of ['chat-form', 'advanced-action']) { const button = new Element('button'); button.type = 'submit'; ids.get(name).append(button); }
const heading = new Element('section');
const document = { getElementById: id => ids.get(id), createElement: tag => new Element(tag), querySelector: () => heading,
  querySelectorAll: selector => selector === '[data-applicability]' ? applicabilityInputs : [] };
const requests = [];
let handler = async path => path.startsWith('/items?') ? { items: [], total: 0 } : null;
const context = vm.createContext({ document, URL, Map, Set, Symbol, Promise, console,
  crypto: { randomUUID: () => 'synthetic-request-1234' }, setTimeout,
  fetch: async (path, options) => { requests.push({ path, ...options }); const data = await handler(path, options); return { ok: data !== null, status: data === null ? 401 : 200, json: async () => ({ data, error: data === null ? { code: 'AUTH_REQUIRED' } : null }) }; }
});
vm.runInContext(fs.readFileSync('web/app.js', 'utf8'), context);
const evaluate = script => vm.runInContext(script, context);
async function settled() { for (let count = 0; count < 100; count++) { await new Promise(resolve => setImmediate(resolve)); if (!evaluate('state.busy.size')) return; } throw Error('UI did not settle'); }
async function main() {
  await settled();
  evaluate("state.actor = 'buyer'; state.csrf = 'synthetic-csrf'; state.selected = { item_id: 'demo-a' };");
"""


@unittest.skipUnless(NODE, "Node is required to execute the HTML event handlers")
class HtmlRequirementsTests(unittest.TestCase):
    def javascript(self, body):
        result = subprocess.run([NODE, "-e", HARNESS + body + "\n}\nmain().catch(error => { console.error(error); process.exitCode = 1; });"],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_checklist_connects_each_clause_to_its_cited_source_and_snapshot(self):
        self.javascript(r"""
  evaluate(`renderChecklist({classification: 'CONDITIONAL', revision: 'index-version-2', applicability: {country:'KR'},
    documents: [{ title: 'Synthetic terms', version_id: 'doc-version-7' }],
    conditions: [{text: '<img src=x onerror=alert(1)>Confirm with provider', evidence_ids:['used','uncited','missing']}],
    cited_evidence_ids: ['used'], evidence: {used: {number:7, title:'Synthetic terms', type:'PDF', source_type:'synthetic_document', page:3,
      version_id:'doc-version-7', url:'https://source.example/terms.pdf', excerpt:'Synthetic clause'},
      uncited: {title:'UNUSED SECRET', url:'https://unselected.example/'}}, readiness:{status:'INDEX_READY'}});`);
  const checklist = ids.get('checklist');
  assert.match(checklist.textContent, /index-version-2/);
  assert.match(checklist.textContent, /doc-version-7/);
  assert.match(checklist.textContent, /country|국가/);
  assert.match(checklist.textContent, /\[7\]/);
  assert.match(checklist.textContent, /3쪽/);
  assert.match(checklist.textContent, /합성 시연 문서/);
  assert.match(checklist.textContent, /<img src=x/);
  assert.doesNotMatch(checklist.textContent, /UNUSED SECRET/);
  const links = checklist.querySelectorAll('a');
  assert.equal(links.length, 1); assert.equal(links[0].href, 'https://source.example/terms.pdf');
  assert.equal(links[0].rel, 'noopener noreferrer');
  assert.ok(html.includes('value="contracts"'));
""")

    def test_source_links_reject_executable_local_or_ambiguous_urls(self):
        self.javascript(r"""
  for (const url of ['javascript:alert(1)', 'data:text/html,secret', 'http://source.example/', 'https://user:password@source.example/',
      'https://source.example:444/', 'https://source.example/a b', 'https://source.example/\nprivate']) {
    context.unsafe = url; assert.equal(evaluate('sourceLink(unsafe)'), null, url);
  }
  evaluate("renderChecklist({classification:'NEEDS_INFORMATION',conditions:[],evidence:[],cited_evidence_ids:[]})");
  assert.match(ids.get('checklist').textContent, /확인 가능한 근거를 찾지 못했습니다/);
  assert.equal(ids.get('checklist').querySelectorAll('a').length, 0);
""")

    def test_classification_without_conditions_still_shows_its_actual_source(self):
        self.javascript(r"""
  evaluate(`renderChecklist({classification:'NOT_TRANSFERABLE',conditions:[],cited_evidence_ids:['negative-clause'],
    evidence:[{evidence_id:'negative-clause',number:2,title:'Synthetic non-transfer clause',source_type:'synthetic_document',
      url:'https://source.example/negative-terms',section:'Transfer prohibition'}]})`);
  const checklist = ids.get('checklist');
  assert.match(checklist.textContent,/Synthetic non-transfer clause/);
  assert.match(checklist.textContent,/Transfer prohibition/);
  assert.equal(checklist.querySelectorAll('a')[0].href,'https://source.example/negative-terms');
""")

    def test_contract_form_submits_revision_payload_through_existing_csrf_api(self):
        self.javascript(r"""
  const trade = {trade_id:'demo-trade', version:4, status:'DRAFT', funded:false, contract:{revision:2, hash:'server-hash',D:1000,W:80,
      terms:{currency:'KRW',settlement_unit:1,evidence_standard:'SYNTHETIC bilateral confirmation',confirmation_deadline:null,
        return_conditions:['mutual_agreement'],dispute_rule:'assigned administrator proposal and bilateral consent'}}};
  context.syntheticTrade = trade;
  handler = async path => path === '/trades/demo-trade/contracts' || path === '/trades/demo-trade' ? trade : {};
  ids.get('trade-action').value = 'contracts'; evaluate('renderTrade(syntheticTrade)');
  ids.get('action-contract-d').value = '1200'; ids.get('action-deadline').value = '2026-11-01';
  ids.get('action-return-conditions').value = 'Synthetic return condition\nSecond condition';
  ids.get('advanced-action').fire('submit'); await settled();
  const request = requests.find(request => request.path === '/trades/demo-trade/contracts');
  assert.ok(request); assert.equal(request.method,'POST'); assert.equal(request.credentials,'same-origin');
  assert.equal(request.headers['X-CSRF-Token'],'synthetic-csrf'); assert.ok(request.headers['Idempotency-Key']);
  const payload = JSON.parse(request.body);
  assert.equal(payload.expected_version,4); assert.equal(payload.D,1200); assert.equal(payload.W,80);
  assert.equal(payload.terms.evidence_standard,'SYNTHETIC bilateral confirmation');
  assert.equal(payload.terms.confirmation_deadline,'2026-11-01');
  assert.deepEqual(payload.terms.return_conditions,['Synthetic return condition','Second condition']);
  assert.equal(payload.terms.dispute_rule,'assigned administrator proposal and bilateral consent');
  assert.match(ids.get('action-hint').textContent,/양측 계약 승인/);
  trade.funded = 1; evaluate('renderTrade(syntheticTrade)');
  assert.equal(ids.get('action-contract-d').disabled,true); assert.equal(ids.get('action-contract-w').disabled,true);
""")

    def test_item_and_actor_drafts_are_isolated_and_old_view_is_invalidated(self):
        self.javascript(r"""
  ids.get('question').value = 'Question draft for A'; applicabilityInputs[0].value = 'KR'; evaluate('saveItemInputs()');
  const contextA = evaluate('viewContext()');
  evaluate("state.selected = {item_id:'demo-b'}; state.viewEpoch++; restoreApplicability('demo-b')");
  assert.equal(ids.get('question').value,''); assert.equal(applicabilityInputs[0].value,'');
  ids.get('question').value = 'Question draft for B'; evaluate('saveItemInputs()');
  evaluate("state.selected = {item_id:'demo-a'}; state.viewEpoch++; restoreApplicability('demo-a')");
  assert.equal(ids.get('question').value,'Question draft for A'); assert.equal(applicabilityInputs[0].value,'KR');
  context.oldView = contextA; assert.equal(evaluate('isCurrentView(oldView)'),false);
  evaluate("state.actor = 'seller'; restoreApplicability('demo-a')");
  assert.equal(ids.get('question').value,''); assert.equal(applicabilityInputs[0].value,'');
""")

    def test_generated_contract_payload_revises_real_service_and_preserves_funded_prices(self):
        from fastapi.testclient import TestClient
        from warranty.application import build_application
        from warranty.config import Settings
        from warranty.interfaces.api import create_app

        payload = json.loads(self.javascript(r"""
  evaluate(`state.trade = {trade_id:'demo-trade',version:1,funded:false,contract:{D:1200,W:80,terms:{
    evidence_standard:'SYNTHETIC bilateral confirmation',return_conditions:['mutual_agreement'],
    dispute_rule:'assigned administrator proposal and bilateral consent'}}};`);
  ids.get('trade-action').value = 'contracts'; evaluate('actionTemplate()');
  ids.get('action-deadline').value = '2026-11-01';
  console.log(evaluate('JSON.stringify(state.actionPayload())'));
"""))
        with ExitStack() as cleanup:
            base = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="warranty-html-contract-")))
            application = build_application(Settings(base / "data", base / "config"))
            cleanup.callback(application.rag.vectors.close)
            with TestClient(create_app(application),
                            base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
                headers = {"Origin": "http://localhost"}

                def login(actor):
                    response = client.post("/session", json={"actor": actor}, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]

                login("buyer")
                item_id = client.get("/items").json()["data"]["items"][0]["item_id"]
                created = client.post("/trades", json={"item_id": item_id, "buyer_id": "buyer", "seller_id": "seller", "D": 1000, "W": 80}, headers=headers)
                self.assertEqual(created.status_code, 201, created.text)
                trade = created.json()["data"]
                sequence = 0

                def mutate(route, data, expected_status=200):
                    nonlocal trade, sequence
                    sequence += 1
                    response = client.post(f"/trades/{trade['trade_id']}/{route}", json={**data, "expected_version": trade["version"]},
                                           headers={**headers, "Idempotency-Key": f"synthetic-html-{sequence}"})
                    self.assertEqual(response.status_code, expected_status, response.text)
                    trade = client.get(f"/trades/{trade['trade_id']}").json()["data"]
                    return response

                original_hash = trade["contract"]["hash"]
                mutate("contracts", payload)
                self.assertEqual(trade["contract"]["revision"], 2)
                self.assertNotEqual(trade["contract"]["hash"], original_hash)
                self.assertEqual(trade["contract"]["D"], 1200)
                self.assertEqual(trade["contract"]["terms"], payload["terms"])
                for actor in ("buyer", "seller"):
                    login(actor)
                    mutate("contract-consents", {key: trade["contract"][key] for key in ("revision", "hash")})
                login("buyer")
                mutate("deposit", {})
                response = mutate("contracts", {**payload, "D": 1201}, 409)
                self.assertEqual(response.json()["error"]["code"], "FUNDED_CONTRACT_IMMUTABLE")
                mutate("contracts", {**payload, "terms": {**payload["terms"], "confirmation_deadline": "2026-12-01"}})
                self.assertEqual(trade["contract"]["revision"], 3)
                self.assertEqual(trade["contract_consents"], [])
                self.assertFalse(trade["receipt"]["received"])

    def test_login_forbidden_trade_refresh_clears_previous_actor_data(self):
        self.javascript(r"""
  evaluate("state.trade = {trade_id:'private-trade'}"); ids.get('trade-content').hidden = false;
  ids.get('trade-json').textContent = 'PREVIOUS ACTOR PRIVATE DATA'; ids.get('actor').value = 'outsider';
  handler = async path => path === '/session' ? {actor:'outsider',csrf_token:'outsider-token'} : path === '/runtime' ? {} : path.includes('/history') ? [] : null;
  ids.get('login').fire('click'); await settled();
  assert.equal(ids.get('trade-content').hidden,true); assert.equal(ids.get('trade-json').textContent,'');
  assert.equal(evaluate('state.trade'),null); assert.equal(ids.get('question').value,'');
""")

    def test_consent_displays_the_selected_proposals_immutable_refund_terms(self):
        self.javascript(r"""
  context.syntheticTrade = {trade_id:'demo-trade',version:4,contract:{revision:3,D:1000,W:80,terms:{return_conditions:['NEXT DRAFT REASON']}},
    proposals:[{proposal_id:'proposal-original',revision:1,hash:'immutable-hash',contract_revision:2,
      allocations:[{component:'W',pay_won:0,refund_won:80}],consents:[],conditions:{refund_reason:'ORIGINAL AGREED REASON',
        review_id:'review-original',evidence_id:'evidence-original',resolution_context:['resolution-original']}}]};
  ids.get('trade-action').value = 'settlement-consents'; evaluate('renderTrade(syntheticTrade)');
  // The DOM selects the first/only option in a real browser.
  ids.get('action-proposal').value = 'proposal-original'; ids.get('action-proposal').fire('change');
  const summary = ids.get('action-fields').textContent;
  assert.match(summary,/ORIGINAL AGREED REASON/); assert.doesNotMatch(summary,/NEXT DRAFT REASON/);
  for (const snapshot of ['immutable-hash','review-original','evidence-original','resolution-original']) assert.ok(summary.includes(snapshot));
  const payload = JSON.parse(evaluate('JSON.stringify(state.actionPayload())'));
  assert.deepEqual(payload,{proposal_id:'proposal-original',revision:1,hash:'immutable-hash'});
""")

    def test_inflight_chat_cannot_repopulate_views_after_actor_switch(self):
        self.javascript(r"""
  let finishChat;
  handler = async path => path === '/chat' ? await new Promise(resolve => { finishChat = resolve; }) :
    path === '/session' ? {actor:'seller',csrf_token:'seller-token'} : path === '/runtime' ? {} : path.includes('/history') ? [] : {};
  ids.get('question').value = 'BUYER PRIVATE QUESTION'; ids.get('chat-form').fire('submit');
  await new Promise(resolve => setImmediate(resolve)); assert.ok(finishChat);
  ids.get('actor').value = 'seller'; ids.get('login').fire('click');
  for (let tries = 0; tries < 100 && evaluate("state.busy.has('login')"); tries++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(evaluate('state.actor'),'seller');
  finishChat({answer:'BUYER PRIVATE ANSWER',classification:'CONDITIONAL',cited_evidence_ids:[],evidence:[]}); await settled();
  assert.doesNotMatch(ids.get('chat-log').textContent,/BUYER PRIVATE/);
  assert.doesNotMatch(ids.get('evidence').textContent,/BUYER PRIVATE/);
""")

    def test_logout_revokes_session_and_clears_sensitive_views(self):
        self.javascript(r"""
  handler = async () => ({logged_out:true});
  ids.get('trade-json').textContent = 'PRIVATE TRADE'; ids.get('chat-log').textContent = 'PRIVATE HISTORY';
  ids.get('logout').fire('click'); await settled();
  const request = requests.find(request => request.path === '/logout');
  assert.ok(request); assert.equal(request.headers['X-CSRF-Token'],'synthetic-csrf');
  assert.equal(evaluate('state.actor'),null); assert.equal(evaluate('state.csrf'),null);
  assert.equal(ids.get('logout').disabled,true); assert.equal(ids.get('trade-json').textContent,'');
  assert.doesNotMatch(ids.get('chat-log').textContent,/PRIVATE HISTORY/);
""")
