/**
 * Prop Edge — saved parlays web app.
 *
 * Paste this into Extensions → Apps Script of a Google Sheet, deploy it as a web app
 * (Execute as: Me, Who has access: Anyone), and put the /exec URL in
 * PARLAY_WEBHOOK_URL in site/index.html. Full steps: README → "Saved parlays".
 *
 * The site POSTs one JSON body per saved slip:
 *   {created, week, legs:[{prop_id, player, market, line, side, price, model_prob}],
 *    book_odds, model_prob}
 * and this appends one row to the "Parlays" sheet:
 *   id | created | week | legs_json | book_odds | model_prob | status
 *
 * "status" starts as "open". Every build reads the rows back as CSV from this same
 * web app (GET <exec URL>?format=csv), grades every parlay, and shows the results
 * in the site's Parlays tab; it does not write back here. No sheet sharing or
 * publish-to-web is needed.
 *
 * After pasting a new version of this file: Deploy → Manage deployments → ✏️ Edit →
 * Version: New version → Deploy. The /exec URL stays the same.
 */

var SHEET_NAME = 'Parlays';
var HEADERS = ['id', 'created', 'week', 'legs_json', 'book_odds', 'model_prob', 'status'];
var MAX_LEGS = 20;

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(10000);   // two saves at once must not collide on the same row
  try {
    var body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    var legs = body.legs;
    if (!Array.isArray(legs) || legs.length === 0 || legs.length > MAX_LEGS) {
      return json_({ok: false, error: 'legs must be a non-empty array (max ' + MAX_LEGS + ')'});
    }
    // keep only the fields we expect, so the sheet can't be stuffed with arbitrary data
    var clean = legs.map(function (l) {
      return {
        prop_id: String(l.prop_id || ''),
        player: String(l.player || ''),
        market: String(l.market || ''),
        line: Number(l.line),
        side: String(l.side || ''),
        price: l.price === null || l.price === undefined || l.price === '' ? null : Number(l.price),
        model_prob: l.model_prob === null || l.model_prob === undefined ? null : Number(l.model_prob)
      };
    });
    var sheet = sheet_();
    var id = Utilities.getUuid().slice(0, 8);
    sheet.appendRow([
      id,
      String(body.created || new Date().toISOString()),
      body.week === undefined || body.week === null ? '' : Number(body.week),
      JSON.stringify(clean),
      body.book_odds === undefined || body.book_odds === null ? '' : Number(body.book_odds),
      body.model_prob === undefined || body.model_prob === null ? '' : Number(body.model_prob),
      'open'
    ]);
    return json_({ok: true, id: id});
  } catch (err) {
    return json_({ok: false, error: String(err)});
  } finally {
    lock.releaseLock();
  }
}

// GET <exec URL>?format=csv  -> the Parlays sheet as CSV (what the build's grader reads)
// GET <exec URL>               -> health check: {"ok":true,"sheet":"Parlays","rows":N}
function doGet(e) {
  var sh = sheet_();
  if (e && e.parameter && e.parameter.format === 'csv') {
    var values = sh.getDataRange().getValues();
    var csv = values.map(function (row) {
      return row.map(function (v) {
        var s = v instanceof Date ? v.toISOString() : String(v);
        return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
      }).join(',');
    }).join('\n');
    return ContentService.createTextOutput(csv).setMimeType(ContentService.MimeType.CSV);
  }
  return json_({ok: true, sheet: SHEET_NAME, rows: Math.max(0, sh.getLastRow() - 1)});
}

function sheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) {
    sh = ss.insertSheet(SHEET_NAME);
  }
  if (sh.getLastRow() === 0) {
    sh.appendRow(HEADERS);
    sh.setFrozenRows(1);
  }
  return sh;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
