<?php
// orb-bars.php — today's 1-minute price bars for one symbol (default QQQ).
//
// Used by the dashboard's Refresh button to redraw the Concretum Bands chart
// live. Read-only market data: it never places orders or changes anything.
//
// Setup: upload this file to the same folder as orb-refresh.php on your web
// server, then fill in the three values below (copy them from orb-refresh.php,
// which already has them). Never commit real keys to GitHub.

$ALPACA_KEY    = 'PASTE_YOUR_ALPACA_API_KEY_ID_HERE';
$ALPACA_SECRET = 'PASTE_YOUR_ALPACA_SECRET_KEY_HERE';
$TOKEN         = 'PASTE_THE_SAME_TOKEN_THAT_orb-refresh.php_CHECKS';

// Only the dashboard's site may call this from a browser.
header('Access-Control-Allow-Origin: https://patrickobirmingham-eng.github.io');
header('Content-Type: application/json');
header('Cache-Control: no-store');

function fail($code, $msg) {
    http_response_code($code);
    echo json_encode(['error' => $msg]);
    exit;
}

if (!isset($_GET['token']) || !hash_equals($TOKEN, (string) $_GET['token'])) {
    fail(403, 'bad token');
}
$symbol = strtoupper(preg_replace('/[^A-Za-z.]/', '', isset($_GET['symbol']) ? $_GET['symbol'] : 'QQQ'));
if ($symbol === '' || strlen($symbol) > 10) {
    fail(400, 'bad symbol');
}

// From today's 9:30 AM New York time (the regular session open).
$start = new DateTime('today 09:30', new DateTimeZone('America/New_York'));
$url = 'https://data.alpaca.markets/v2/stocks/' . rawurlencode($symbol) . '/bars?' . http_build_query([
    'timeframe'  => '1Min',
    'start'      => $start->format(DateTime::RFC3339),
    'feed'       => 'iex',     // the real-time feed the trading bot uses for today
    'adjustment' => 'raw',
    'limit'      => 10000,
    'sort'       => 'asc',
]);

$ch = curl_init($url);
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT        => 15,
    CURLOPT_HTTPHEADER     => [
        'APCA-API-KEY-ID: ' . $ALPACA_KEY,
        'APCA-API-SECRET-KEY: ' . $ALPACA_SECRET,
    ],
]);
$body = curl_exec($ch);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);
if ($body === false || $code !== 200) {
    fail(502, 'Alpaca request failed (' . $code . ')');
}

$data = json_decode($body, true);
$bars = [];
foreach ((isset($data['bars']) && is_array($data['bars'])) ? $data['bars'] : [] as $b) {
    $bars[] = ['t' => $b['t'], 'o' => $b['o'], 'h' => $b['h'], 'l' => $b['l'], 'c' => $b['c'], 'v' => $b['v']];
}
echo json_encode(['symbol' => $symbol, 'bars' => $bars]);
