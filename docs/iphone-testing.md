# iPhone install and testing checklist

Do this on both iPhones once Family Table runs on the permanent computer with an **HTTPS** address
(for example a Tailscale `https://….ts.net` name). Before starting, set `COOKIE_SECURE=true` in that
server's `.env` and restart it.

Use **Safari**. Other iPhone browsers can add home-screen icons on newer iOS versions, but Safari is the one tested here.

## 1. Install

- [ ] Open the HTTPS address in Safari. The padlock shows, with no certificate warning.
- [ ] Sign in.
- [ ] Tap **Share** (square with an arrow), scroll down, tap **Add to Home Screen**.
- [ ] The preview shows the terracotta bowl-on-a-table icon and the name **Family Table**. Tap **Add**.
- [ ] The icon on the home screen looks sharp, with no black corners or white border.

## 2. Opens like an app

- [ ] Tap the icon. Family Table opens full screen, **with no Safari address bar or toolbar**.
- [ ] The first time, it asks you to sign in. That's expected, because home-screen apps keep their own sign-in. **Keep me signed in** is already ticked. Sign in.
- [ ] The top status bar (time, battery) is readable and doesn't cover the Family Table header.
- [ ] Rotate to landscape: nothing hides behind the notch or Dynamic Island, and nothing sits under the home indicator bar.
- [ ] Swipe the app away in the app switcher, then reopen it from the icon. You're still signed in.
- [ ] Wait a day and reopen it. You're still signed in (it lasts 30 days with Keep me signed in).

## 3. Moving around

- [ ] Recipes, Meal Planner, and Shopping List are at the top, and the bar stays visible while you scroll.
- [ ] Open a recipe, then use **All recipes** to come back (there's no browser back button).
- [ ] Tapping links inside Family Table never opens Safari.
- [ ] A recipe's **Original recipe** link (imported recipes) opens the website in a separate Safari view. Close it to return.

## 4. Recipes

- [ ] Search works, and the keyboard doesn't zoom the page in when you tap the search box.
- [ ] Photos fill their cards with no sideways scrolling.
- [ ] On a recipe, tap ingredients to tick them off while cooking.
- [ ] **Edit recipe**: type in fields, and the page doesn't zoom. The keyboard doesn't hide the field you're typing in.
- [ ] **Add recipe → Import from a website** works, and adding a photo offers the camera and photo library.

## 5. Meal Planner

- [ ] All seven days are visible by scrolling.
- [ ] **Add dinner** → pick a recipe. **Change** and **Remove** work.
- [ ] Drag a dinner by its ⋮⋮ handle onto another day (they swap). Also try the **Move** menu.
- [ ] Previous and next week arrows work.
- [ ] **Generate / Update Shopping List** works.

## 6. Shopping List (both phones open on the list)

- [ ] Phone A adds "Bananas". Within about 5–10 seconds it appears on Phone B without touching anything.
- [ ] Phone B taps a grocery (the box **or** its name). It disappears from Need to Buy at once, and the count goes down.
- [ ] Within about 5–10 seconds Phone A shows the same.
- [ ] Open **Purchased**, tap the item, and it goes back to Need to Buy (undo).
- [ ] **⋯ → Already have it** puts it under **Already have**, separate from Purchased.
- [ ] **Kitchen Staples** opens, and **Add to list** works.
- [ ] Lock the phone for a minute, then unlock it. The list catches up right away.
- [ ] Desktop browser: the same list, and changes show up there too.

## 7. Connection problems

- [ ] Turn on Airplane Mode and open the app: you see **Can't reach Family Table** with **Try again**, not a blank screen.
- [ ] Turn Airplane Mode off and tap **Try again**: the page comes back.
- [ ] Tap a grocery while in a dead spot: a "Not saved yet" message appears and the change is sent when the signal returns. Don't close the app before then.

## 8. Signing out

- [ ] **Sign out** returns to the sign-in page, and the list can't be opened again until you sign in.

## If something's wrong

- Icon or name looks old after an update: delete the home-screen icon (long-press → Remove App → Delete from Home Screen) and add it again.
- The server's address changed: remove the icon and add it again from the new address.
- Opens in Safari with an address bar: make sure it was added with **Add to Home Screen** from the HTTPS address, and opened from the icon.
