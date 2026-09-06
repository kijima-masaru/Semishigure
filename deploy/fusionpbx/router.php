<?php
// Minimal router for PHP's built-in server: serve files that exist, otherwise fall through to the requested .php.
$path = urldecode(parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH));
$file = __DIR__ . $path;
if ($path !== '/' && is_file($file)) { return false; }
if (is_dir($file) && is_file($file . '/index.php')) { require $file . '/index.php'; return true; }
require __DIR__ . '/index.php';
