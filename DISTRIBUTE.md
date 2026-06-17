# Installing DropNote on another iPhone (without this Mac)

The app already talks to the backend over the internet (`app/src/config.ts` →
`http://34.44.0.96:8080`, the GCP VM), so a build installed on **any** iPhone, on **any**
network, will work — no cable, no LAN, no this-Mac. What you need is a way to get a signed
build onto the other phone. Your options depend on whether you have a **paid Apple Developer
account** ($99/yr) or just the **free Personal Team**.

> Before building, make sure `app/src/config.ts` points at a reachable backend (your GCP
> VM's public IP, not a LAN IP). The backend URL is baked into the build — if it changes,
> you must rebuild and redistribute.

> **Build Release, not Debug.** A plain `npx expo run:ios` makes a **Debug** build that
> loads its JavaScript from the Metro packager on your Mac at runtime — off the Mac it shows
> `No script URL provided … unsanitizedScriptURLString = (null)`. A **Release** build embeds
> the JS bundle inside the app so it runs standalone, on any phone, with no laptop:
> ```bash
> cd app
> npx expo run:ios --configuration Release --device
> ```
> The EAS / Xcode-Archive builds below are already Release, so they're standalone by design.

---

## Option A — TestFlight (recommended; needs the paid Apple Developer Program)

True over-the-air install: the other person taps a link, installs Apple's **TestFlight**
app, and gets DropNote. Builds last 90 days; updates are one command. Nothing is tethered to
your Mac.

Easiest path is **EAS Build** (Expo's cloud builder — it also builds for you, so you don't
even need Xcode):

```bash
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
cd app
npm install -g eas-cli          # one-time
eas login                       # your Expo account
eas build:configure             # one-time (eas.json is already in the repo)

# Build in the cloud and upload straight to App Store Connect / TestFlight:
eas build --platform ios --profile production --auto-submit
```

Then in **App Store Connect → your app → TestFlight**, add the tester's email (or share the
public TestFlight link). They install TestFlight from the App Store and tap your link. Done.

Notes:
- First run will ask for your Apple Developer credentials so EAS can create the App ID,
  certificates, and provisioning — it manages all of that for you.
- The `bundleIdentifier` in `app/app.json` (currently `com.voicememory.app`) must be unique
  to your account; change it if it's taken (e.g. `com.<you>.dropnote`).
- Prefer Xcode? **Xcode → Product → Archive → Distribute App → TestFlight & App Store** does
  the same thing without EAS.

---

## Option B — Free (no paid account): build an `.ipa`, recipient sideloads it

With only the **free Personal Team** you can't use TestFlight/ad-hoc, but you can hand over
an **`.ipa` file** that the other person installs with **their own Apple ID** using a
sideloader. Caveat: free-signed apps **expire after 7 days** and must be re-installed/
refreshed (the sideloader tools automate this), and the recipient needs a computer once.

**1. Produce the `.ipa`** (pick one):

- **EAS (cloud, no Xcode):**
  ```bash
  cd app
  eas build --platform ios --profile preview
  ```
  When it finishes, download the `.ipa` from the build page (the CLI prints the URL).

- **Xcode (local):** open `app/ios/*.xcworkspace` → set your Team under Signing →
  **Product → Archive** → **Distribute App → Release Testing / Ad Hoc → Export** → you get
  a `.ipa`. (A free team works for a personal export; the 7-day limit still applies.)

**2. The recipient installs the `.ipa`** on their iPhone using one of these on *their*
computer (not yours):

- **[Sideloadly](https://sideloadly.io)** (Win/Mac) — plug the iPhone into their computer,
  drag in the `.ipa`, sign in with *their* Apple ID, click Start.
- **[AltStore](https://altstore.io)** — installs the `.ipa` and auto-refreshes it over Wi-Fi
  so it doesn't die after 7 days.

After install, on the iPhone: **Settings → General → VPN & Device Management → trust** the
developer profile.

---

## Which should you pick?

| | Paid ($99/yr) | Free Personal Team |
|---|---|---|
| Method | **TestFlight** (Option A) | **Sideload `.ipa`** (Option B) |
| Recipient effort | Install TestFlight, tap link | Run Sideloadly/AltStore on their computer |
| Expiry | 90 days, 1-command refresh | 7 days, must re-sign/refresh |
| Needs your Mac? | No | No (recipient uses their own computer) |

For anything beyond a quick test, **Option A / TestFlight** is far less hassle. Option B is
the zero-cost route.

---

## After the backend IP changes
The VM's external IP is **ephemeral** — stop/start can change it. If it does: update
`app/src/config.ts`, rebuild (`eas build …` or Xcode Archive), and redistribute. Reserve a
**static IP** (or put a domain + HTTPS in front — see [DEPLOY.md](DEPLOY.md)) to avoid this.
