# Run report — lookup_member_and_get_savings_balance

**Run id:** `replay_lookup_member_and_get_savings_balance_20260915T040036_2874a8`  
**Goal:** Look up a member by ID and return their current savings balance.  
**Target:** 127.0.0.1_5050 (web)  
**Started:** 2026-09-15T04:00:36.903363+00:00 · **Duration:** 0.6s · **Outcome:** ❌ Hard failure — `locator_unresolved`

## Steps

### 1. navigate — s0
![screenshot](screenshots/step1.png)
Navigated to /.
Locator: — · Verified: ✓ · URL: `http://127.0.0.1:5050/login`

### 2. type — s1
![screenshot](screenshots/step2.png)
Typed {{username}} into textbox "Username" — entered 'operator1'.
Locator: primary · Verified: ✓ · URL: `http://127.0.0.1:5050/login`

### 3. type — s2
![screenshot](screenshots/step3.png)
Typed {{password}} into textbox "Password" — entered «redacted».
Locator: primary · Verified: ✓ · URL: `http://127.0.0.1:5050/login`

### 4. click — s3
![screenshot](screenshots/step4.png)
Clicked button "Log In".
Locator: primary · Verified: ✓ · URL: `http://127.0.0.1:5050/members/search`

### 5. navigate — s0
![screenshot](screenshots/step5.png)
Navigated to /.
Locator: — · Verified: ✓ · URL: `http://127.0.0.1:5050/members/search`

### 6. type — s1
![screenshot](screenshots/step6.png)
Typed {{member_id}} into textbox "Search by Member ID, Name, or partial SSN" — entered '99999'.
Locator: primary · Verified: ✓ · URL: `http://127.0.0.1:5050/members/search`

### 7. click — s2
![screenshot](screenshots/step7.png)
Clicked button "Search".
Locator: primary · Verified: ✓ · URL: `http://127.0.0.1:5050/members/search`

### 8. click — s3
![screenshot](screenshots/step8.png)
Clicked link "10001" — target not found.
Locator: — · Verified: ✗ · URL: `http://127.0.0.1:5050/members/search`

## Result

**Status:** hard_failure

**Outcome code:** `locator_unresolved`

**Failed at step:** `s3`


Step s3: expected role+name='link:{{member_id}}' on http://127.0.0.1:5050/members/search, but no element resolved. Observed instead: cell:Member Servicing Portal — Credit Union Back Office, cell:Member Search Signed on: operator1 — Logout, link:Member Search, link:Logout, heading:Member Search, textbox:Search by Member ID, Name, or partial SSN, button:Search, cell:No matching member found. This is a normal search result, not an error.
