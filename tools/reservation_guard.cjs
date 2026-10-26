// Invoke at the start of an existing Node AP script, before any AP access:
// const code = require('./ha-wax/reservation_guard.cjs').guard(__filename, 600, [0, 10]);
// if (code !== null) process.exit(code);
// Keep the configuration and Python helpers next to this file.
const path = require('node:path');
const { spawnSync } = require('node:child_process');

function guard(script, timeout = 600, releaseCodes = [0]) {
  const absolute = path.resolve(script);
  if (process.env.NETGEAR_WAX_RESERVED_SCRIPT === absolute) {
    delete process.env.NETGEAR_WAX_RESERVED_SCRIPT;
    return null;
  }
  const result = spawnSync('python3', [
    path.join(__dirname, 'with_reservation.py'),
    '--config', path.join(__dirname, 'config.json'),
    '--timeout', String(timeout),
    '--release-codes', releaseCodes.join(','),
    '--', process.execPath, absolute, ...process.argv.slice(2),
  ], {
    stdio: 'inherit',
    env: { ...process.env, NETGEAR_WAX_RESERVED_SCRIPT: absolute },
  });
  return Number.isInteger(result.status) ? result.status : 75;
}
module.exports = { guard };
