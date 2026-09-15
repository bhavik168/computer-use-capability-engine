# Run report — lookup_member_and_get_savings_balance

**Run id:** `replay_lookup_member_and_get_savings_balance_20260915T040049_55c4b9`  
**Goal:** Look up a member by ID and return their current savings balance.  
**Target:** 127.0.0.1_5050 (web)  
**Started:** 2026-09-15T04:00:49.318684+00:00 · **Duration:** 0.59s · **Outcome:** ⚠️ Business outcome — `permission_denied`

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
Typed {{member_id}} into textbox "Search by Member ID, Name, or partial SSN" — entered '10005'.
Locator: primary · Verified: ✓ · URL: `http://127.0.0.1:5050/members/search`

### 7. click — s2
![screenshot](screenshots/step7.png)
Clicked button "Search".
Locator: primary · Verified: ✓ · URL: `http://127.0.0.1:5050/members/search`

### 8. click — s3
![screenshot](screenshots/step8.png)
Clicked link "10001".
Locator: primary · Verified: ✓ · URL: `http://127.0.0.1:5050/members/10005`

## Result

**Status:** business_outcome

**Outcome code:** `permission_denied`


Known outcome permission_denied detected
