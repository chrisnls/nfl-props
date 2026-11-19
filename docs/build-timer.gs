/**
 * Prop Edge — reliable build timer.
 *
 * GitHub's own schedule for this repo drops or delays most runs (3 of ~14 NBA runs on Nov 17), so builds,
 * line pulls and grading happen late. Apps Script time triggers run on time, so this file starts the two
 * build workflows every hour instead. GitHub's schedule stays on as a backup.
 *
 * Each run uses lines mode "auto": it only spends Odds API credits when a pull is actually due (no lines
 * yet, lines stale, or a game starting within 2 hours). Most runs are free rebuilds that refresh stats,
 * Kalshi prices and news, and grade finished games and saved parlays.
 *
 * SETUP (once, about 5 minutes):
 *  1. In the same Apps Script project as the parlay web app: ➕ next to "Files" → Script → name it
 *     "build-timer" → paste this whole file → 💾.
 *  2. Make a GitHub token that can ONLY start this repo's workflows:
 *     github.com → Settings → Developer settings → Personal access tokens → Fine-grained tokens →
 *     Generate new token. Repository access: "Only select repositories" → chrisnls/nfl-props.
 *     Permissions → Repository permissions → Actions: "Read and write" (nothing else). Expiration: up to you.
 *  3. In Apps Script: ⚙️ Project Settings → Script Properties → Add script property:
 *     Property = GITHUB_TOKEN, Value = the token. (Script properties are private to you; the token is
 *     never in the site or the repo.)
 *  4. Back in the editor, pick "setupTimer" in the function dropdown → ▶ Run → approve the permissions
 *     prompt (it asks to "connect to an external service" = api.github.com, and to manage triggers).
 *  5. Optional check: run "triggerBuildsNow" once, then look at the repo's Actions tab: two runs appear.
 *
 * To stop it: run "removeTimer", or delete the trigger under ⏰ Triggers.
 */

var REPO = 'chrisnls/nfl-props';
var WORKFLOWS = ['nba.yml', 'build.yml'];       // NBA, NFL
var TZ = 'America/Chicago';
var FIRST_HOUR = 6, LAST_HOUR = 23;              // local hours to run (6am-11pm CT; 6am catches overnight grading)

function setupTimer() {
  removeTimer();
  ScriptApp.newTrigger('hourlyBuilds').timeBased().everyHours(1).create();
  Logger.log('Hourly build timer installed.');
}

function removeTimer() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'hourlyBuilds') ScriptApp.deleteTrigger(t);
  });
}

function hourlyBuilds() {
  var hour = Number(Utilities.formatDate(new Date(), TZ, 'H'));
  if (hour < FIRST_HOUR || hour > LAST_HOUR) return;
  triggerBuildsNow();
}

function triggerBuildsNow() {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) throw new Error('Add a GITHUB_TOKEN script property first (see SETUP in build-timer).');
  WORKFLOWS.forEach(function (wf) {
    var r = UrlFetchApp.fetch('https://api.github.com/repos/' + REPO + '/actions/workflows/' + wf + '/dispatches', {
      method: 'post',
      contentType: 'application/json',
      headers: {Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'},
      payload: JSON.stringify({ref: 'main', inputs: {lines: 'auto'}}),
      muteHttpExceptions: true
    });
    var code = r.getResponseCode();
    Logger.log(wf + ' -> ' + code + (code === 204 ? ' (started)' : ' ' + r.getContentText().slice(0, 200)));
  });
}
