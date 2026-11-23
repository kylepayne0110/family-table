# iPhone install and testing checklist

Do this on both iPhones once Family Table runs with its **HTTPS** address (the Tailscale Funnel
`https://….ts.net` name). Both `docker compose up --build` and `./scripts/serve.sh` already use
HTTPS-only cookies, so nothing needs changing in `.env`.

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
- [ ] Leave the planner open past midnight (or set the phone's clock ahead is **not** enough: the server's household time zone decides). The next morning, yesterday's dinner has left its day and appears under **Earlier this week**.
- [ ] **Add ingredients** on a dinner opens a short checklist as a sheet. Staples start unchecked. The button reads "Add 1 item", "Add 3 items", and is disabled with nothing checked. Double-tapping it adds the items once.

## 5b. Scan a packaged meal (on the HTTPS address)

- [ ] On an open day, tap **Scan a packaged meal** → **Scan the barcode**. Safari asks for the camera; allow it. Point at a frozen meal's barcode: it's read within a few seconds and the name, brand, and size appear.
- [ ] Check the details match the box; fix anything wrong. If no directions were found, tap **Take a photo** and photograph the cooking panel.
- [ ] **Add to …**, then open the planner on the other phone: the meal is on that day; open it and tap the photo to enlarge it.
- [ ] Deny the camera (Settings → Safari → Camera → Deny), reload, tap **Scan the barcode**: a message explains, and **Take a photo of the barcode** and typed digits still work.
- [ ] Try it on cellular data (Wi-Fi off) with Funnel running.
- [ ] On a day that already has a dinner, the page warns you, and nothing is replaced unless you tick the box.

## 5a. Planned dinner and Cooking Assistant

- [ ] Tap a planned dinner's name: the recipe opens with **Cooking for** −/+ buttons. Changing servings updates the amounts straight away; **Reset** goes back.
- [ ] **Cooking Assistant** opens full screen. **Back to recipe** returns to the same scroll position.
- [ ] Type "How much chicken do I need tonight?" and "How many cups are in one US quart?".
- [ ] Stop Ollama on the computer and ask a question that needs the model: a clear "can't reach the local cooking model" message appears, and servings and **Add ingredients** still work.

## 5c. Cook with me (voice), about 10 minutes, phone propped up as if cooking

Use a recipe with several steps, planned for today. Turn the volume up.

1. [ ] Open the dinner from the Meal Plan and tap **Cook with me**. Safari asks for the microphone (and speech recognition): **Allow**. You hear "How can I help?" and the circle says **Listening**.
2. [ ] Say "Walk me through it." It reads **only step 1**, then waits. The step is highlighted on the recipe.
3. [ ] Say "next." Step 2. Say "repeat that." Step 2 again. Say "go back." Step 1.
4. [ ] While it's reading a step, say "How much flour?" (use an ingredient in the recipe). It stops talking, answers with tonight's amount, and says you're still on that step. Say "next": it continues from the right step.
5. [ ] Tap the circle while it's talking: it stops at once and listens.
6. [ ] Say "Make this for eight tonight." The **Cooking for** number on screen changes to 8.
7. [ ] Say "Read me the whole recipe." It reads all ingredients and steps (this is the only time it reads everything).
8. [ ] Say "From now on, use 3/4 cup of flour in the original recipe" (or a swap like "from now on, use panko"). It describes the change and asks whether to save it. Say "no, cancel": nothing changes. Ask again and say "yes, save it": it says Saved, and **Recipe card** shows the new amount for the original servings.
9. [ ] Check it never treats its own voice as a command: it shouldn't answer itself after it finishes talking.
10. [ ] Lock the phone for 30 seconds, unlock: the panel says **Paused**. Tap the circle and say "repeat": the same step as before.
11. [ ] Close Safari completely, reopen the dinner: the conversation is in Cooking Assistant, and Cook with me offers to pick up at the same step.
12. [ ] **Mute**: the microphone indicator in the status bar goes away. **End**: the panel closes and the microphone is released.
13. [ ] Settings → Safari → Microphone → Deny for this site, then tap Cook with me: it says the microphone is off; **Next / Repeat / Back** and typing still work.

Note anything misheard or slow; it helps tune the phrases it understands.

## 5d. Tell me the recipe (voice on Add a recipe), about 5 minutes

1. [ ] **Recipes → + Add → Tell me the recipe.** Say, in one go: "These are Grandma's chicken nuggets. They feed four. I use two chicken breasts, cut them into chunks, and coat them in breadcrumbs." The name, **Created by** (Grandma), Servings (4), two ingredients, and two steps fill in as you talk, without interrupting you.
2. [ ] Say "Actually, make that three chicken breasts." The same row changes to 3.
3. [ ] Say "I don't measure the salt," then "that's it." It asks one question at a time, e.g. about the breadcrumbs' amount, and offers "to taste" for the salt. Answer, and say "skip" for times.
4. [ ] Type something into Description while the helper is open, then keep talking: your typing stays.
5. [ ] Tap **Undo**: the last change goes back.
6. [ ] Say "That sounds good," tap **Close**: nothing has been added to Recipes yet. Leave the page and come back to Add a recipe: it offers **Pick up where you left off**, with a chosen photo still there.
7. [ ] Press **Save recipe**: only now does it join the recipe book.
8. [ ] Try it on a recipe from **Import from a website**: corrections apply to the imported fields.

## 5e. Accounts and Tables (two phones)

- [ ] Phone A: tap the Table name in the dark bar → **Make an invite link** → Share/Copy it to Phone B.
- [ ] Phone B (signed out): open the link. It says "**Kyle invited you to Payne's Table**". **Create an account and join**: it lands straight in Payne's Table with the same recipes.
- [ ] Phone A: **Table settings → Members → Remove** Phone B's person (read the confirmation). Phone B, on the Meal Plan, shows "You no longer have access to this Table" within about 10 seconds.
- [ ] Phone B: start a second Table from Welcome; now tapping the Table name shows a switcher with a check by the current one.

## 6. Shopping List (both phones open on the list)

- [ ] Phone A adds "Bananas". Within about 5–10 seconds it appears on Phone B without touching anything.
- [ ] Phone B taps a grocery (the box **or** its name). It disappears from Need to Buy at once, and the count goes down.
- [ ] Within about 5–10 seconds Phone A shows the same.
- [ ] Open **Purchased**, tap the item, and it goes back to Need to Buy (undo).
- [ ] Buy two items, open **Purchased**, tap **Clear purchased**. The section disappears on both phones, and Need to Buy is unchanged.
- [ ] Remove a dinner in the Meal Planner. Nothing already on the list changes.
- [ ] **⋯ → Already have it** puts it under **Already have**, separate from Purchased.
- [ ] **Kitchen Staples** opens, and **Add to list** works.
- [ ] Lock the phone for a minute, then unlock it. The list catches up right away.
- [ ] Desktop browser: the same list, and changes show up there too.

## 7. Connection problems

- [ ] Turn on Airplane Mode and open the app: you see **Can't reach Family Table** with **Try again**, not a blank screen.
- [ ] Turn Airplane Mode off and tap **Try again**: the page comes back.
- [ ] Tap a grocery while in a dead spot: a "Not saved yet" message appears and the change is sent when the signal returns. Don't close the app before then.

## 8. Signing out

- [ ] **Account → Sign out of this device** returns to the sign-in page, and the list can't be opened again until you sign in.
- [ ] **Account → Signed-in devices** lists both phones; **Sign out** next to the other one signs it out.

## If something's wrong

- Icon or name looks old after an update: delete the home-screen icon (long-press → Remove App → Delete from Home Screen) and add it again.
- The server's address changed: remove the icon and add it again from the new address.
- Opens in Safari with an address bar: make sure it was added with **Add to Home Screen** from the HTTPS address, and opened from the icon.
