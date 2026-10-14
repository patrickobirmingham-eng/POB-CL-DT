<?php
// spreads-settings-save.php — saves the credit-spreads bot's rules from the
// Credit Spreads dashboard (Rules button) into spreads_settings.json on main.
//
// Setup: upload this file next to orb-settings-save.php on your web server and
// fill in the two values below. Copy them from orb-settings-save.php, which
// already has them: the same GitHub token and the same passcode work here.
// Never commit the real values to GitHub.
//
// Safety: it only accepts the rule names listed in $FIELDS below, checks every
// value is in a sane range, keeps every other key in the file as it is, and
// needs the passcode on every save. It cannot touch any other file, the day
// bot's settings, or the API keys.

$GITHUB_TOKEN = 'PASTE_THE_GITHUB_TOKEN_FROM_orb-settings-save.php';
$PASSCODE     = 'PASTE_THE_PASSCODE_FROM_orb-settings-save.php';

$REPO   = 'patrickobirmingham-eng/POB-CL-DT';
$PATH   = 'spreads_settings.json';
$BRANCH = 'main';

header('Access-Control-Allow-Origin: https://patrickobirmingham-eng.github.io');
header('Access-Control-Allow-Methods: POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');
header('Content-Type: application/json');
header('Cache-Control: no-store');
if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') { http_response_code(204); exit; }

function fail($code, $msg) {
    http_response_code($code);
    echo json_encode(['ok' => false, 'error' => $msg]);
    exit;
}
if ($_SERVER['REQUEST_METHOD'] !== 'POST') fail(405, 'POST only');

$body = json_decode(file_get_contents('php://input'), true);
if (!is_array($body)) fail(400, 'bad request');
if (!isset($body['token']) || !hash_equals($PASSCODE, (string) $body['token'])) fail(403, 'wrong passcode');
$in = isset($body['settings']) && is_array($body['settings']) ? $body['settings'] : null;
if ($in === null) fail(400, 'no settings sent');

// name => [type, min, max]
$FIELDS = [
    'ENABLED'                 => ['bool'],
    'MIN_DTE'                 => ['int', 1, 120],
    'MAX_DTE'                 => ['int', 1, 120],
    'TARGET_DTE'              => ['int', 1, 120],
    'MIN_DELTA'               => ['num', 0.01, 0.5],
    'MAX_DELTA'               => ['num', 0.01, 0.5],
    'TARGET_DELTA'            => ['num', 0.01, 0.5],
    'MIN_CREDIT_PCT_OF_WIDTH' => ['num', 0, 0.9],
    'MIN_NATURAL_PCT_OF_MID'  => ['num', 0, 1],
    'TAKE_PROFIT_PCT'         => ['num', 0.05, 0.95],
    'STOP_LOSS_MULT'          => ['num', 0.25, 10],
    'MAX_LOSS_PER_SPREAD'     => ['num', 100, 200000],
    'MAX_OPEN_SPREADS'        => ['int', 0, 100],
    'MAX_PER_SYMBOL'          => ['int', 0, 20],
    'MAX_PER_SECTOR'          => ['int', 0, 100],
    'MAX_NEW_PER_DAY'         => ['int', 0, 50],
    'ROLL_ENABLED'            => ['bool'],
    'ROLL_MIN_DTE_LEFT'       => ['int', 0, 120],
    'ROLL_TARGET_DTE'         => ['int', 1, 120],
    'MAX_ROLLS'               => ['int', 0, 10],
    'AVOID_EARNINGS'          => ['bool'],
    'CLOSE_BEFORE_EXPIRY_DTE' => ['int', 0, 30],
    'OPEN_START_ET'           => ['time'],
    'OPEN_END_ET'             => ['time'],
    'WIDTH_TABLE'             => ['widths'],
    'WATCHLIST'               => ['watchlist'],
];

$clean = [];
foreach ($in as $k => $v) {
    if (!isset($FIELDS[$k])) fail(400, "unknown rule $k");
    $f = $FIELDS[$k];
    switch ($f[0]) {
        case 'bool':
            if (!is_bool($v)) fail(400, "$k must be on or off");
            break;
        case 'int':
        case 'num':
            if (!is_int($v) && !is_float($v)) fail(400, "$k must be a number");
            if ($f[0] === 'int' && floor($v) != $v) fail(400, "$k must be a whole number");
            if ($v < $f[1] || $v > $f[2]) fail(400, "$k must be between {$f[1]} and {$f[2]}");
            $v = $f[0] === 'int' ? (int) $v : (float) $v;
            break;
        case 'time':
            if (!is_string($v) || !preg_match('/^(0[9]|1[0-5]):[0-5][0-9]$/', $v)) fail(400, "$k must be a time between 09:00 and 15:59");
            break;
        case 'widths':
            if (!is_array($v) || count($v) < 1 || count($v) > 15) fail(400, 'width table must have 1-15 rows');
            foreach ($v as $i => $row) {
                if (!is_array($row) || count($row) !== 4) fail(400, 'each width row needs 4 numbers');
                foreach ($row as $n) if ((!is_int($n) && !is_float($n)) || $n < 0 || $n > 1000000) fail(400, 'bad number in width table');
                if ($row[0] >= $row[1]) fail(400, 'width row ' . ($i + 1) . ': price "from" must be below "to"');
                if ($row[2] <= 0 || $row[2] > $row[3] || $row[3] > 500) fail(400, 'width row ' . ($i + 1) . ': min width must be above 0 and not above max width');
            }
            break;
        case 'watchlist':
            if (!is_array($v) || count($v) < 1 || count($v) > 400) fail(400, 'watchlist must have 1-400 symbols');
            foreach ($v as $sym => $sec) {
                if (!preg_match('/^[A-Z][A-Z.]{0,5}$/', (string) $sym)) fail(400, "bad symbol $sym");
                if (!is_string($sec) || !preg_match('/^[A-Za-z0-9 &\/-]{1,30}$/', $sec)) fail(400, "bad category for $sym");
            }
            break;
    }
    $clean[$k] = $v;
}

function gh($method, $url, $token, $data = null) {
    $ch = curl_init($url);
    $hdr = ['Authorization: Bearer ' . $token, 'User-Agent: spreads-settings-save', 'Accept: application/vnd.github+json'];
    if ($data !== null) {
        $hdr[] = 'Content-Type: application/json';
        curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($data));
    }
    curl_setopt_array($ch, [CURLOPT_CUSTOMREQUEST => $method, CURLOPT_HTTPHEADER => $hdr,
                            CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 20]);
    $out = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    return [$code, json_decode($out, true)];
}

$api = "https://api.github.com/repos/$REPO/contents/$PATH";
list($code, $file) = gh('GET', "$api?ref=$BRANCH", $GITHUB_TOKEN);
if ($code !== 200 || !isset($file['sha'])) fail(502, "could not read $PATH from GitHub ($code)");
$current = json_decode(base64_decode($file['content']), true);
if (!is_array($current)) fail(502, "$PATH on GitHub is not valid JSON");

$new = array_merge($current, $clean);
foreach ([['MIN_DTE', 'TARGET_DTE', 'MAX_DTE'], ['MIN_DELTA', 'TARGET_DELTA', 'MAX_DELTA']] as $t) {
    if (!($new[$t[0]] <= $new[$t[1]] && $new[$t[1]] <= $new[$t[2]])) fail(400, "{$t[0]} <= {$t[1]} <= {$t[2]} is required");
}
if ($new['OPEN_START_ET'] >= $new['OPEN_END_ET']) fail(400, 'OPEN_START_ET must be before OPEN_END_ET');
if ($new === $current) { echo json_encode(['ok' => true, 'unchanged' => true]); exit; }

$json = json_encode($new, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_PRESERVE_ZERO_FRACTION) . "\n";
list($code, $res) = gh('PUT', $api, $GITHUB_TOKEN, [
    'message' => 'Update credit-spreads rules from the dashboard [skip ci]',
    'content' => base64_encode($json),
    'sha'     => $file['sha'],
    'branch'  => $BRANCH,
]);
if ($code !== 200 && $code !== 201) fail(502, "GitHub refused the save ($code)" . (isset($res['message']) ? ': ' . $res['message'] : ''));
echo json_encode(['ok' => true, 'commit' => isset($res['commit']['sha']) ? substr($res['commit']['sha'], 0, 7) : '']);
