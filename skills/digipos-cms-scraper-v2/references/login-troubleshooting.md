# Login Troubleshooting — DigiPOS CMS

## Password Pitfall

The password is `TselDigipos123` (14 characters). **NO exclamation mark.**

Early in the session, the password was incorrectly recorded as `TselDigipos123!` (with `!`). This caused:
- Login failures in both browser tool and standalone Playwright
- Misleading "Username or Password is incorrect" error from the server
- Confusion because the `!` was attributed to shell escaping when the actual issue was the wrong password

**Lesson:** When a password works in one context (browser tool) but fails in another (Playwright script), verify the credentials are *exactly* the same. Don't assume shell escaping issues without verifying.

## API vs Browser Tool Session Differences

The standalone Playwright script initially rejected login (HTTP 422) even with correct credentials. The browser tool worked fine with the same credentials. This was investigated extensively:

- User-Agent headers were identical (HeadlessChrome/148)
- Cookie structure was similar
- The `webdriver` flag was set to `true` in both
- A Basic Auth header (`ZORA484:ZORA2016`) was being sent automatically in Playwright but not in the browser tool
- Ultimately, the `!` password issue was the real cause

**Resolution:** Using the correct password (`TselDigipos123` without `!`) made the Playwright script work correctly. The Basic Auth header was a red herring.

## Login Redirect Path

The site's login handler (`$.ajax` POST to `/login-post`) redirects to `/home` on success, NOT to `/deposit/monitoring-riwayat`. The script must:
1. Wait for `**/home` URL change after clicking Sign in
2. Then manually navigate to `/deposit/monitoring-riwayat`

This is different from what you might expect — most sites redirect to the page you were trying to access.

## jQuery vs Native Selectors

The DigiPOS CMS uses jQuery extensively. Several selectors that work with jQuery (`:contains()`, `.trigger()`) do NOT work with native `document.querySelectorAll()`:

| Pattern | jQuery | Native Equivalent |
|---------|--------|-------------------|
| Text match | `$('button:contains("CSV")')` | `document.querySelectorAll('button')` + filter by text |
| Click | `$('btn').trigger('click')` | `document.querySelector('btn').click()` |
| Val + event | `$('input').val(x).trigger('change')` | `input.value = x; input.dispatchEvent(new Event('change'))` |

For Playwright `page.evaluate()`, use jQuery (available on the page) rather than native JS for these patterns.
