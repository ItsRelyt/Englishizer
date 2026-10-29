#!/usr/bin/env python3
"""
Writing Assistant 5 - offline, system-wide spelling, grammar & style helper.

Runs in the background and checks the WHOLE text field you are in (not just what
you typed): it reads the focused field through the OS accessibility APIs, so text
that was already there, pasted text, autocorrect and mouse edits are all seen, and
every fix is verified against the real field before and after it is applied.
Where an app doesn't expose its text, it falls back to following your typing, and
Ctrl+Alt+A can scan such a field on demand. A small floating card (plus in-place
underlines where possible) offers fixes you apply by click or hotkey. 100% local:
no network calls. Field text is kept only in memory (wiped after a few idle
minutes), never written to disk, and password fields are never read.

What changed in v5 (rewrite)
  * Engine is a pure function of the text (check_text) - no incremental
    state to drift out of sync. `--check "text"` runs it without any GUI.
  * Fixed: the built-in word list had 221 word pairs fused by missing spaces.
  * Fixed: Fix-all could DELETE words (it applied style hints); it now only
    applies spelling/grammar/punctuation fixes that are safe.
  * Fixed: removed the Ctrl+A/Ctrl+C "probe" that was sent into whatever app
    had focus (dangerous in terminals) and the clipboard "recovery" that could
    delete text. Fixes are now injected only when the tracked caret is
    trustworthy, with minimal edits, and any surprise resets the session.
  * Fixed: junk entries in the typo / confusion tables; US-only spelling is
    gone (colour/organise are fine; choose "us" or "uk" in config if you care).
  * New: ~40 contextual grammar rules (your/you're, its/it's, than/then,
    a/an with real sound rules, agreement, participles...), personal dictionary
    hot-reload, config file, ignored apps (terminals, password managers),
    clipboard check (Ctrl+Alt+V) that is always safe, light/dark card.

Install:   pip install pynput pyspellchecker
Optional:  pip install screeninfo     (multi-monitor placement)
           Windows: pip install uiautomation   (reads browsers, Word, modern apps)
           macOS underlines: pip install pyobjc-framework-ApplicationServices pyobjc-framework-Cocoa
Without pyspellchecker the app falls back to /usr/share/dict/words if present,
else to a small built-in list that only flags well-known misspellings.

Hotkeys (hold the modifiers from config, default Ctrl+Alt):
  1 / 2 / 3  apply suggestion     N / B  next / previous issue
  D  ignore                        W  add word to dictionary
  F  fix all (safe fixes only)     P  pause / resume
  C  collapse / expand card        V  check the clipboard text
  A  scan this field now (copies it via Ctrl+A/Ctrl+C - only for apps that can't be read;
     your clipboard is restored and the caret ends at the end of the field)
  O  open dictionary folder        Q  quit

Dictionaries: ~/WritingAssistant/dictionaries/ - every .txt/.dic/.csv file is
loaded (one word per line, '#' comments; Hunspell and "word count" lists work)
and reloaded automatically. `python writing_assistant.py --import list.txt`.
Settings live in ~/WritingAssistant/config.json.

Limits worth knowing: which apps can be read depends on the OS. Windows: classic
edit controls always; browsers/Word/modern apps need `pip install uiautomation`.
macOS: native Cocoa text fields/views (grant Accessibility permission); Electron
and some browsers don't expose text. Linux: no field reading yet (typed tracking +
Ctrl+Alt+A). For unreadable fields it follows your typing and resets whenever it
cannot be sure where your caret is. Wayland-native apps do not deliver key events.
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import tkinter as tk
    from tkinter import font as tkfont
except Exception:                       # headless / no Tk: engine + --check still work
    tk = tkfont = None
try:
    from pynput import keyboard, mouse
except Exception:
    keyboard = mouse = None

VERSION = "5.0"
WIN = sys.platform == "win32"
MAC = sys.platform == "darwin"
DEBUG = "--debug" in sys.argv or bool(os.environ.get("WA_DEBUG"))


def dbg(*a):
    """Debug log. Never prints typed characters."""
    if DEBUG:
        print("[wa]", *a, flush=True)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
HOME_DIR = Path.home() / "WritingAssistant"
DICT_DIR = HOME_DIR / "dictionaries"
MY_WORDS = DICT_DIR / "my_words.txt"
CONFIG_PATH = HOME_DIR / "config.json"
DICT_EXTS = {".txt", ".dic", ".csv", ".tsv"}

DEFAULT_IGNORE_APPS = [
    # terminals (typing there is commands, and cursor injection is unsafe)
    "cmd", "powershell", "pwsh", "windowsterminal", "wt", "conhost", "terminal",
    "iterm2", "iterm", "gnome-terminal", "gnome-terminal-server", "konsole", "xterm",
    "alacritty", "kitty", "wezterm", "wezterm-gui", "tilix", "ghostty", "putty",
    # password managers / secrets
    "keepass", "keepassxc", "1password", "bitwarden", "lastpass", "dashlane",
    "enpass", "keychain access", "passwords",
]
DEFAULTS = {
    "idle_check_seconds": 0.5,        # re-check this long after the last keystroke
    "check_trailing_word_seconds": 1.2,  # ...and include the word you are still typing
    "forget_after_seconds": 300,      # wipe the in-memory buffer after this much idle
    "enter_resets": True,             # Enter starts a fresh buffer (safe for chat apps)
    "style_level": "standard",        # off | light | standard | full
    "spelling": "any",                # any | us | uk  (uk covers AU/NZ)
    "underlines": True,               # in-place marks where the app exposes its caret
    "theme": "dark",                  # dark | light
    "hotkey_modifiers": ["ctrl", "alt"],
    "ignore_apps": DEFAULT_IGNORE_APPS,
    "max_buffer": 1800,               # typed-only tracking window (apps whose text can't be read)
    "read_field": True,               # read the focused field via accessibility APIs (ground truth)
    "max_field_chars": 30000,         # analyse at most this much of one field (window around the caret)
    "windows_filter_injected": True,  # Windows: treat other programs' synthetic keys as "unknown edit"
}


def _type_ok(v, dv) -> bool:
    if isinstance(dv, bool):
        return isinstance(v, bool)
    if isinstance(dv, (int, float)):
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, type(dv))


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                for k, dv in DEFAULTS.items():
                    v = raw.get(k, dv)
                    ok = _type_ok(v, dv)
                    if ok:
                        cfg[k] = v
        else:
            HOME_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2), encoding="utf-8")
    except Exception as ex:
        print("[wa] config problem, using defaults:", ex)
    if cfg["style_level"] not in ("off", "light", "standard", "full"):
        cfg["style_level"] = "standard"
    if cfg["spelling"] not in ("any", "us", "uk"):
        cfg["spelling"] = "any"
    if cfg["theme"] not in ("dark", "light"):
        cfg["theme"] = "dark"
    mods = [m for m in cfg["hotkey_modifiers"] if m in ("ctrl", "alt", "shift", "cmd")]
    cfg["hotkey_modifiers"] = mods or ["ctrl", "alt"]
    cfg["max_buffer"] = max(300, min(int(cfg["max_buffer"]), 20000))
    return cfg


CFG = dict(DEFAULTS)     # replaced by load_config() in main()

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def match_case(original: str, repl: str) -> str:
    if not repl:
        return repl
    if len(original) > 1 and original.isupper() and original.isalpha():
        return repl.upper()
    if original[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def edit_distance(a: str, b: str) -> int:
    """Optimal-string-alignment distance (a transposition costs 1)."""
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def minimal_edit(old: str, new: str):
    """(offset, delete_len, insert_text) turning old into new with the fewest keystrokes."""
    p = 0
    m = min(len(old), len(new))
    while p < m and old[p] == new[p]:
        p += 1
    q = 0
    while q < m - p and old[len(old) - 1 - q] == new[len(new) - 1 - q]:
        q += 1
    return p, len(old) - p - q, new[p:len(new) - q]


def text_is_safe_to_navigate(s: str) -> bool:
    """Arrow keys move one grapheme; only trust our counting for plain characters."""
    for c in s:
        o = ord(c)
        if o > 0xFFFF or 0x300 <= o <= 0x36F or o in (0x200D, 0xFE0F) or (o < 32 and c not in "\n\t"):
            return False
    return True

# --------------------------------------------------------------------------
# Lexicon: built-in core words + pyspellchecker corpus + system dictionary
# --------------------------------------------------------------------------
# (frequency, words) - correct spacing verified by tests.
_CORE_TIERS = [
    (100000000,
     """
     a after also and any as back because by can come day do even first for give go he her how i
     in is it its look make me most new now of on only other our over say see than that the then
     these think this to two us use want was way well will with work you
     """),
    (10000000,
     """
     above action animal area article ask ball beach beauty become bed begin believe bird black
     blue break bring brown call camera camp card change child circle class command complete
     complex condition continue country course court create depth desert design dog door dream
     drop easy enemy exact experience face fact failure fair far fast feel find finish firm
     follow friendly fruit full gold grow happen head help here high hold house idea important
     include instrument interest island item keep kind lake land lead learn leave let like line
     listen live lose lot machine man mean measure meet message metal might moment money move
     music need night note observe offer often orange order paint pair pay person photo place
     plan plane plant play point police power price primary provide put question race radio read
     receive record region remain road rock rose sail seat seem send separate service set share
     short show shy side sing sit smile smoke snow song space speak speech spring stand start
     state stop strength student summer sun swing talk teach tell thing try turn understand unit
     view watch weight wind write year yes yet
     """),
    (1500000,
     """
     though
     """),
    (1000000,
     """
     ability abstract absurd accurate acquaintance activist afternoon ally amateur amazing anger
     apartment apple apprentice arm artificial audience autumn awesome awful baby banana bank
     bathroom battle beautiful bedroom beginner belt berry bill bitter blood blunt body bone boy
     brain bread bridge bright brother budget building business butter button cake capacity
     career cash cent century ceo certain chair chairman chance cheap cheese chicken chocolate
     citizen city clean clear clever client close clothing cloud coat coffee coin cold colleague
     college color comfort common company competitor concrete constant continent cookie cool
     correct cost courage coworker crowd cultural cure curved customer cute danger dark daughter
     decade deep defeat development diet difficult director dirty disease distant doctor dollar
     dress drink dry duty ear earth economic economy educated education egg elegant emotion
     emotional employee employer energy equality ethical evening excellent exercise expense
     expensive experienced expert extraordinary eye factory fake fall false family fancy farm
     father fear feeling fight financial finger fish flat floor follower food foolish foot
     foreigner forest founder freedom frequent fresh friend funny furniture garden general
     genuine girl government graceful gradual grape gray green group growth guest hair hand
     handsome happiness hard hat hate health heart heavy hidden honesty honor hope horrible
     hospital host hot hour human ignorant illness impossible income individual industry
     inexperienced intelligence investment investor irrational jacket job joy juice justice kid
     kindness kitchen knee knowledge knowledgeable lamp leader leg legal lemon lesson light
     lightning likely logical long loss love lovely low loyalty luxurious manager market master
     meat medicine melon member mental milk mind minute misery mob modest month moon moral
     morning mother mountain mouth narrow nation natural near neck neighbor noon normal nose
     novice nurse observer obvious ocean odd office oil opponent opportunity ordinary owner pain
     pants parent park participant particular partner pasta patient peace people pepper personal
     physical pie pink plain pleasure pocket political poor possible potential poverty practical
     precise president pretty prevention private probable profession professional professor
     profit progress public purple qualified quick rain rapid rare rational real reasonable red
     religious research resident respect responsibility rest rice rich ridiculous right risk
     rival river roof room rough round sadness safety salad salt salty sauce school scientific
     sea season second secret sensible serious shallow shareholder sharp shirt shoe shop simple
     sister skill skilled skin sky sleep slow smart smooth social sock soft son soul soup sour
     special specific spectator spice spirit spiritual square star steady stomach store storm
     straight strange stranger street struggle study stupid success sudden sugar sure sweet
     synthetic table talent tall tea teacher team teammate technical terrible theoretical thick
     thin thunder today tomorrow tooth town trade trained treatment true trust ugly unclear
     uneducated unique university unskilled unsure untrained usual vague value vegetable victory
     village vinegar virtual volunteer wall war warm water weakness wealth weather week weird wet
     white wide window wisdom wise wonderful world worth wrong yard yellow yesterday
     """),
    (800000,
     """
     writing
     """),
    (500000,
     """
     written
     """),
    (250000,
     """
     access
     """),
    (200000,
     """
     advice definitely successful whose
     """),
    (150000,
     """
     immediately independent threw truly won
     """),
    (120000,
     """
     chose stairs thrown
     """),
    (100000,
     """
     academic accountant actor administrator advisor advocate ambassador amount analysis analyst
     app arbitrator architect art artist assistant attendant attorney audio auditor author baked
     baker banker bartender beaten billionths birthday blended blog boiled broker browser builder
     bureaucrat butcher candidate canned captain carpenter cashier chapter character chat chef
     chopped clerk coach code comment commissioner composer computer conclusion consultant
     consumer contractor cook cooked coordinator cored council counselor cut data database
     defender delegate designer detective developer device diced digit digital diplomat directory
     distributor document dried driver eighths electrician electronic email engine engineer
     entrepreneur essay executive farmer figure file fisherman folded folder font format
     freelancer fried frozen gardener governor grade graphic grated grilled halved handmade
     hardware helper homemade hulled hundredths hunter husked hybrid image information innovator
     instant instructor intellectual internet introduction investigator judge keyboard kneaded
     lawyer layout lecturer legislator letter level link logger manufacturer mason mayor mechanic
     mediator mentor merchant microwaved millionths minced miner mixed mobile moderator mouse
     musician negotiator network novel number officer official offline online onsite operator
     organic organizer painter paragraph path peeled philosopher phone phrase pilot pitted
     planner plumber poem poet politician portable porter post presenter printer producer program
     prosecutor psychiatrist psychologist quality quantity quartered query rancher rating raw
     receptionist rechargeable remote renewable representative researcher retailer review roasted
     rolled sailor scanner scholar scientist score screen script search secretary section seeded
     senator sentence server shelled shredded singer sixteenths sliced smartphone software
     soldier speaker spelling standard statistic steamed stemmed stirred story strategist style
     summary supplier sustainable symbol taxpayer technician teller template tenths text
     therapist thinker thousandths trader trainer trillionths tutor url vegan vegetarian vendor
     video voter waiter waitress webpage website welder whisked wholesaler wireless word writer
     """),
    (80000,
     """
     calendar embarrass february wednesday
     """),
    (60000,
     """
     cite insure occurrence rhythm
     """),
    (50000,
     """
     typo
     """),
    (30000,
     """
     wary
     """),
    (20000,
     """
     changeable harass weary
     """),
    (10000,
     """
     accept achieve address advise affect afford agree allow answer appear apply argue arrange
     assert assume attach attempt attend avoid await base bear beat behave belong bend bet bind
     bite blame bleed bless blink bloom blow board boast boil bolt borrow bounce bow box brag
     breathe brew bribe broadcast brush bubble build bump burn burst bury bust buy calculate
     carry catch cause challenge charge chart chase cheat check cheer chew choose claim clap
     clash clasp click climb cling clip clock clutch collect comb communicate compare compete
     complain compose compute conceal concede concentrate confess confirm confront confuse
     connect conquer consider consist construct consult consume contain contract contribute
     control convert convince cooperate copy correlate cough count cover crack craft crash crawl
     credit creep criticize crop cross crown crush cry cultivate curl current curve cycle damage
     dance dare deal debate decide declare decline decorate decrease dedicate delay deliver
     demand demonstrate deny depend describe deserve desire destroy detail detect determine
     develop devote diagnose die differ dig dilute dine direct disagree disappear discover
     discuss dislike dismiss display dispute distribute dive divide divorce dominate donate doubt
     draft drag drain draw drift drill drip drive drown drum dump dust earn ease eat echo edit
     eject elect eliminate embrace emerge emit employ empty enable enclose encounter encourage
     end endure enforce engage enhance enjoy enlarge enrich enroll ensure enter entertain escape
     establish estimate evaluate examine exceed exchange excite exclude excuse exhaust exhibit
     exist exit expand expect experiment explain explode explore export expose express extend
     extinguish extract fail fare feed fetch fill finance fire fit fix flash flee float flock
     flow flower fluently flush fly fold force forecast forge forget forgive form found frame
     free freeze fret frighten frost frown fry fuel fulfill function fund gain gamble gather gaze
     generate get giggle glow govern grab graduate greet grind grip groan guard guess guide
     hammer handle hang harbor harm harvest hatch haul have heal hear heat hide hike hire hit
     hook hover hug hunt hurry hurt identify ignore illustrate imagine imitate implement imply
     import impose improve increase indicate induce influence inform inhabit inherit initiate
     inject injure inspect inspire install instruct insult integrate intend interpret interrupt
     interview introduce invade invent invest investigate invite involve iron issue join joke
     journey juggle jump justify kick kill kiss kneel knit knock know label laugh launch lay leak
     lean leap lease lend lengthen lessen license lift limit list load loan locate lock lodge log
     loose lower mail maintain manage mark marry match matter melt mention mine miss mix mobilize
     mock modify monitor mount mourn multiply murmur muse mutter name narrate navigate neglect
     negotiate nest net notice notify nourish obey object obtain occupy occur omit open operate
     oppose opt organize originate output overcome overheat overlook overtake owe pack paddle
     pardon part participate pass paste patch patrol pause pave peek peel perceive perform permit
     persist photograph pick piece pierce pin pitch plead plot plow plug plunge polish ponder
     populate pose position possess postpone pour practice praise pray preach predict prefer
     prepare present preserve press pretend prevent print proceed process produce project promise
     promote propose protect protest prove publish pull pump punch purchase pursue push puzzle
     qualify quit quote raise rally rank rate reach react realize reason recall recognize
     recommend recover recruit redeem reduce refer reflect reform refuse regard regret regulate
     rehearse reinvent relate relax release relieve rely remember remind remove renew rent repair
     repeat replace reply report request require rescue resemble reserve resist resolve respond
     restore restrict result resume return reveal reverse revise reward ride ring rinse rise roar
     roast rob roll rotate route row ruin rule run rush sack sample satisfy save saw scale scan
     scatter schedule scold scope scream scrub seal secure seek seize select sell sense serve
     settle sew shade shake shape sharpen shave shed shelter shift shine ship shock shoot shout
     shrink shrug shut sigh sight sign signal simplify sink sip sketch ski skip slam slap slice
     slide slip smash smell smite snap sneeze sniff snore soak soar solve sorry sort sound spar
     spare spark specialize specify spend spill spin spit split spoil spot spray spread sprint
     squeeze stack staff stage stain stake stall stamp stare starve stay steal steam steer step
     stick sting stir stitch stock strain stress stretch stride strike string strip stroke strum
     stuff stumble submit subscribe subtract succeed suck suffer suggest suit summarize supply
     support suppose surf survive suspect suspend swallow sway swear sweat sweep swell swim
     switch sword tackle tag take tap target taste tear tease telephone tempt tend test thank
     thicken thread threaten thrive throw thumb tick tie tighten tilt time tip tire toast toe
     toggle toll tone tool top torch toss total touch tour tow trace track trail train transfer
     transform translate transmit transport trap travel tread treat trend trial trip trot trouble
     tug tumble tune twist type undergo undertake unite unlock update upgrade uphold upload upset
     urge utilize vacation validate vanish vary venture verify visit voice vote wait wake walk
     wander warn wash waste wave weaken wear weave weep weigh welcome west whisper whistle widen
     wink wipe wire wish withdraw witness wonder worry worship wound wrap wreck yawn yearn yell
     yield zap zoom
     """),
    (1000,
     """
     aboard about abroad according across actually additionally adjacent afterwards again against
     ago ahead albeit alike alive all almost alone along alongside already alright although
     altogether always am amazed among amongst an another anybody anyhow anyone anything anytime
     anywhere apart apparently approximately are around aside asleep assisting at attentive
     available away backwards bad badly bag balanced bar barely basic basically bath be became
     becoming been before beforehand began beginning begins behind being belief below beneath
     beside besides best better between beyond big bit blank blessed blind block blown boat bold
     book boolean born both bottom bought bound brave breath breeze brief briefly brilliant broad
     broke broken built busy but cage calm came cannot cap capable car cardinal careful carefully
     case cast categories category caught caution cautious cave cease central certainly chain
     chalk changes chasing cheek cheerful cherry chest chief children choice choices chosen
     church cigarette cities civil clam clarify classes classic cleanliness clearly cliff climate
     closely closer cloth clothes cloudy club clue coast collapse collar collection combo comedy
     comfortable comic coming commerce commercial commission commit commonly community compact
     comparison compass competition competitive compile completely complexity comply compound
     compromise concept concern concerned concert conclude conduct conference confidence
     confident conform congratulations connection conscious consequence consequently considerable
     consistent consistently constantly constraint consultation contact content contest context
     continuous contrary contrast convenient conversation cope core corner corporation correctly
     correspond cotton could county couple cousin crazy creation creative creature crew crime
     criminal crisis crisp criteria critical crucial cruel crumb cup curious currently
     """),
    (100,
     """
     daily dangerous darkness darling dash date dawn days dead deadly dear death decent decision
     deck deed deeply deer definition degree delete deliberate delicate delicious delight
     democracy demon dent department dependent deposit derive description desk despite
     destruction determined devil diagram dial dialogue diameter diamond diary dick dictation
     dictionary did difference different differently difficulty dinner direction directly dirt
     disability disabled disappointed disaster discipline discount discussion dish disorder
     distance distinct distinguish distract district disturb diverse division does doing doll
     domain domestic don done doom dose double down downstairs downtown downward dozen drama
     dramatic drawn dressed drew driven drowned drug drunk due dull during each eager early
     easily east eastern echoed edge edition editor educate effect effective effort eight either
     elder electrical element eleven else emergency eminent emperor empire employed employment
     endanger endless endorse energetic engagement english enormous enough entire entity entrance
     envelope environment envy equal equally equipment erase error especially essential estate
     event eventually ever every everybody everyone everything everywhere evidence evil exactly
     example except exception excess exciting execute existing exotic experimented exploitation
     exploration explosion exposure extension extra extremely eyes facilities factor faint fairly
     faith fallen falsehood fame familiar famous fantasy fashion fashionable faster fat fate
     favorite feast feature fed federal fee fellow female fence ferry festival fever few fiber
     fiction field fierce fifteen fifty film filter final finally fine fiscal five flag flame
     flaw fleet flesh flight flood flour fluent fluid flutter foam focus fog folk following fool
     football foreign forever fork formal former formula forth fortune forty forward fought foul
     foundation four fourth fox fraction fragile frank fraud frog front fully fun fundamental
     funeral further future game garage garlic gas gate gathered gay gear gender generous
     gentleman gesture giant gift glad glance glass glimpse global glove goal goat god golden
     gone good goodbye goose gorgeous got grace gradually grain grand grandfather grandma grant
     graph grasp grass grateful grave great greatest greed grey grief grieve grill grim grin grit
     grocery ground grown grumble guilt gun gym habit had hail half hall hallway handful happy
     harder hardly hare harmony harness harsh has haste hawk hay hazard headline healthcare
     healthy heap heard heaven height hell hello hen hence herb herd hero herself hi hid hill
     hint hip his historic history hive hoard hob hole holiday hollow holy home honest honey hood
     hoof horn horse hotel however huge hull humble humor hundred hung hunger hurdle hurricane
     husband hut hydrogen hyphen ice icon id identity idle if ignorance ill illegal imagination
     immediate immensely immigrant impact impatient impress impulse incentive inch incidence
     including indeed index indication indirect indoor inevitable infant infection inflation
     ingredient initial initiative inner innocent input insertion insist instance instead
     instinct institute insurance intact intake integrity intense intent interact interior
     intermediate internal international interruption interval intimate into intolerant intrusion
     intuition invalid invented invincible invisible invitation invoke inward irregular itself
     jail jam jar jaw jazz jelly jet joint journal jungle junior junk jury just keen kettle key
     kids king kit kite knew knife knight knot known koala lab labor laboratory lack lady lag
     lamb language lantern lap large largely last late later laughter law lawn layer lazy leaf
     league least leather led left length lens less lest lever liability lick lid lie life
     lifetime likelihood limb lime limp lion lip liquid literal litter little lively liver living
     loaf local logic lonely longitude lost loud luck luggage lump lunch lying mad magazine magic
     maid main major male mall manner mansion manual map marble march margin marriage married
     marshal mask mass mat mate material math matrix maximum may maybe meal meaning meanwhile
     medal media medical medium meeting melody memory mend merely mess metabolic microwave
     midnight migrate mild mile military mill million mineral miniature minimum minister minor
     minority miracle mirror mistake mixture moan mode modern moist molecule monkey monster mood
     more motion motive mourning movie much mud murder muscle museum mushroom must mutual my
     mystery myth nail naked nasty national native nature naval neat necessary negative neither
     nervous neutral never news next nice nine no noble nobody nod noise noisy none nonsense
     north notable notebook nothing noun nowhere nuclear null numeric nun nut oak oar obligation
     obscure observation occasion occasional occupation octopus odder off offense offensive old
     older olive omega one onion onto opera operation opinion opposite optimize option or orbit
     organ origin otherwise ought ounce out outbreak outcome outdoors outer outfit outline
     outrage outset outside oval oven overall overdue overhead overhear overlap overseas
     overthrow overtime own ox pace page painful palace pale palm pan panda panel paper parade
     parallel parameter parcel parish partial partly passage passion past pastry patience pattern
     payment peaceful peak peanut pear peasant peck pedal penny perfect perfume perhaps period
     permanent permission persuade pet phase phenomenon philosophy piano picnic picture pig pile
     pill pinch pine pint pipe pity pizza planet plank planned plastic plate platform playground
     please plenty plum plural plus poach poetry poison pole policy polite pond pony pool pop
     popular population porridge port portion portrait positive potato pound preacher precious
     predicate pregnant prejudice preliminary premium preparation prescribe presence pressure
     presume prettify previous prey pride priest primacy prime prince princess principal
     principle prior prison privilege proactive problem procedure proclaim product profile
     profound prominent prompt pronounce proof proper property proportion proposal prospect
     prosper protein proud province provoke pudding puff pulse punishment pupil puppet purpose
     purse pyramid qualification quarrel quarter queen queue quiet quite quiz quota quotation
     rabbit raccoon radar radial radiate rag rage raid rail rake ramp range ransom rap rash rat
     rather ratio rave ray ready realm reap rear rebel rebuild receipt recent recipe recognition
     reconcile recur reef reel refine refrain refresh refuge refund regain regime register
     regular reject rejoice relief religion remark remedy reminder remnant removal repent
     repertoire repetition replay represent reproduce reptile republic reputation reservation
     reservoir reset reside resize resolution resort resource restless restraint retail retain
     retreat reunion revenge revenue revive rhyme rib ribbon rid ridge ridicule rife rifle rig
     rigid rim rink riot rip ripe ritual roam robot rocket rod role romantic root rope rot rouse
     routine royal rubber rubble ruby rude rug rumor rural rust rustic rut safe saga said saint
     sake salary sale sales salmon same sand satellite satin satisfied saturate sauna savage
     scandal scarce scare scarf scenario scene scenery scent scheme scholarship science scissors
     scoop scooter scorn scout scramble scrap screw scuba seafood seam seed segment seldom self
     seminar senate senior sensation sensitive sentiment sequence serene series servant session
     seven several severe shack shadow shaft shall shame shark sheep sheet shelf shell shield
     shit shore shot should shove shower shriek shrill shrimp shrine shrub shuffle shutter sick
     siege silence silk silly silver similar sin since sincere single sir site situation six size
     skate skiing skim skull slab slang slate slave sled sleek sleeve slender slick slight slim
     slope slot slug slum slump smack small smog snack snail snake sneer snub soap sober sofa
     soil solar sold sole solemn solid solution some soon soothe sore sorrow source south
     souvenir sow soy spa spam span spawn spear species specimen spectacle spectrum speculate
     speed spell sphere spider spike spine spiral spite splash splendid sponge spool sport spouse
     sprinkle sprout spur spy squash squat squeak squid stable stair stale stalk stance staple
     stark station statue status staunch steak steel steep stem stereo stew stiff still stool
     stoop stout stove strand stream strife stripe strive strong struck structure stuck stud
     stump stun sturdy subject sublime subsidy substitute subtle suburb subway such suffice
     sullen summit summon sunk superb surge surmount surname surpass surplus surprise surrender
     surround survey susceptible sustain swab swagger swamp swap swarm swept swift syllable
     symmetry sync syntax syrup system tab tablet taboo tack tail tailor tale tame tan tank taper
     tar tariff task tavern tax technique teddy tee teen teeth temper temple tempo tenant tender
     tennis tense tent term terrace terror textile texture theft their them theme themselves
     theory there therefore thesis they thief thigh third thirst thorn thorough those thought
     thousand threat three threshold thrill throat throne through thus ticket tide tidy tiger
     tile till timber timid tin tiny tissue title toad together toilet token told tolerance
     tomato tomb tongue tonight too took topic tore torn torpedo tortilla torture tough towel
     tower toy trait tramp trance trash tray treacherous treasure tree trek tremble trench tribe
     trick trim triumph troop tropical trough truck trumpet trunk truth tub tube tuck tuition
     tunnel turbo twelfth twelve twenty twice twig twin twine tyrant umbrella uncle under undo
     unfit unfold union unless unlike unwind up upon upper upright uproar upward urban usage
     useful useless usher utility utmost vacant vacuum vain valid valley valuable vanilla
     variable variant variation varied variety vast vault vector vehicle veil velocity venom verb
     verdant verse vessel vest veteran veto vex via vibe vice victim victor vileness villa vine
     vinyl viola violet virtue vision visualize vital vivid vocal volcano volt volume vowel
     voyage wade waist walnut ward warehouse warp warranty watt wax weak weapon weasel web wedge
     weed welfare whale wheat wheel when where whether which while whim whine whirl whole whom
     why wicked widow width wield wife wig wild willing wilt wing winner winter wit witch without
     woe wolf woman wont wood wool worm worse worst would wrath wreath wrench wrest wretch
     wriggle wring wrist wrote wrought wrung yacht yeast yoke yon young your youth zeal zero
     zigzag zinc zip zone zoo
     """),
]

# Misspellings that must be flagged (even if a big corpus happens to contain
# them) and whose intended word is certain. Values may be multi-word.
TYPO_MAP = {
    "teh": "the", "hte": "the", "tehn": "then", "tihs": "this", "taht": "that",
    "waht": "what", "wiht": "with", "adn": "and", "nad": "and", "htey": "they",
    "thier": "their", "theri": "their", "wich": "which", "whcih": "which",
    "becuase": "because", "becasue": "because", "beacuse": "because",
    "alot": "a lot", "infact": "in fact", "atleast": "at least", "eachother": "each other",
    "aswell": "as well", "everytime": "every time", "noone": "no one",
    "thankyou": "thank you", "alittle": "a little", "inbetween": "in between",
    "absense": "absence", "accomodate": "accommodate", "acheive": "achieve",
    "acknowlege": "acknowledge", "adress": "address", "agressive": "aggressive",
    "apparantly": "apparently", "arguement": "argument", "athiest": "atheist",
    "basicly": "basically", "beggining": "beginning", "begining": "beginning",
    "belive": "believe", "beleive": "believe", "bizzare": "bizarre",
    "buisness": "business", "bussiness": "business", "busness": "business",
    "calender": "calendar", "catagory": "category", "cemetary": "cemetery",
    "changable": "changeable", "collegue": "colleague", "comming": "coming",
    "commitee": "committee", "comitted": "committed", "commited": "committed",
    "concious": "conscious", "definate": "definite", "definately": "definitely",
    "definatly": "definitely", "dissapear": "disappear", "dissapoint": "disappoint",
    "dilema": "dilemma", "embarass": "embarrass", "enviroment": "environment",
    "existance": "existence", "experiance": "experience", "facinating": "fascinating",
    "foriegn": "foreign", "freind": "friend", "frend": "friend", "fourty": "forty",
    "futher": "further", "gaurd": "guard", "goverment": "government",
    "grammer": "grammar", "greatful": "grateful", "guarentee": "guarantee",
    "harrass": "harass", "heighth": "height", "hieght": "height", "hygene": "hygiene",
    "ignorence": "ignorance", "immediatly": "immediately", "independant": "independent",
    "interupt": "interrupt", "irresistable": "irresistible", "knowlege": "knowledge",
    "liason": "liaison", "libary": "library", "maintainance": "maintenance",
    "millenium": "millennium", "miniscule": "minuscule", "mispell": "misspell",
    "neccessary": "necessary", "necesary": "necessary", "neccesary": "necessary",
    "noticable": "noticeable", "occassion": "occasion", "occured": "occurred",
    "occurence": "occurrence", "oppurtunity": "opportunity", "oportunity": "opportunity",
    "orignal": "original", "paralell": "parallel", "parliment": "parliament",
    "persistant": "persistent", "posession": "possession", "prefered": "preferred",
    "privelege": "privilege", "priviledge": "privilege", "probly": "probably",
    "publically": "publicly", "questionaire": "questionnaire", "realy": "really",
    "reccomend": "recommend", "recomend": "recommend", "recieve": "receive",
    "recieved": "received", "reciept": "receipt", "refered": "referred",
    "relevent": "relevant", "religous": "religious", "repitition": "repetition",
    "resistence": "resistance", "restaraunt": "restaurant", "rythm": "rhythm",
    "schedual": "schedule", "sargent": "sergeant", "seperate": "separate",
    "sieze": "seize", "sincerley": "sincerely", "speach": "speech",
    "succesful": "successful", "sucessful": "successful", "supercede": "supersede",
    "suprise": "surprise", "tendancy": "tendency", "tommorow": "tomorrow",
    "tommorrow": "tomorrow", "tomorow": "tomorrow", "truely": "truly",
    "unforseen": "unforeseen", "unfortunatly": "unfortunately", "untill": "until",
    "usefull": "useful", "vaccuum": "vacuum", "vegtable": "vegetable",
    "vehical": "vehicle", "wierd": "weird", "withold": "withhold",
    "writting": "writing", "writeing": "writing", "yeild": "yield",
    "accross": "across", "agianst": "against", "apparant": "apparent",
    "beatiful": "beautiful", "beautifull": "beautiful", "cheif": "chief",
    "decison": "decision", "desicion": "decision", "diffrent": "different",
    "excercise": "exercise", "familar": "familiar", "finaly": "finally",
    "happend": "happened", "intresting": "interesting", "langauge": "language",
    "lenght": "length", "managment": "management", "nieghbor": "neighbor",
    "peice": "piece", "perhaps": "perhaps", "pronounciation": "pronunciation",
    "similer": "similar", "sucess": "success", "tecnology": "technology",
    "thansk": "thanks", "thanx": "thanks", "wether": "whether", "wnat": "want",
    "yestarday": "yesterday", "youre": "you're", "recive": "receive",
    "adressed": "addressed", "occuring": "occurring", "writen": "written",
    "exitement": "excitement", "excitment": "excitement", "goverments": "governments",
}
TYPO_MAP = {k: v for k, v in TYPO_MAP.items() if k != v}

SHORT_WORDS = {"a", "i", "an", "as", "at", "be", "by", "do", "go", "he", "if", "in", "is",
               "it", "me", "my", "no", "of", "on", "or", "so", "to", "up", "us", "we"}

_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
KEY_ADJ: dict = {}
for _r, _row in enumerate(_ROWS):
    for _c, _ch in enumerate(_row):
        _n = set()
        for _dr in (-1, 0, 1):
            _rr = _r + _dr
            if 0 <= _rr < len(_ROWS):
                for _dc in (-1, 0, 1):
                    _cc = _c + _dc
                    if (_dr, _dc) != (0, 0) and 0 <= _cc < len(_ROWS[_rr]):
                        _n.add(_ROWS[_rr][_cc])
        KEY_ADJ[_ch] = _n

LETTERS = "abcdefghijklmnopqrstuvwxyz"


def edits1(w: str) -> set:
    sp = [(w[:i], w[i:]) for i in range(len(w) + 1)]
    out = {a + b[1:] for a, b in sp if b}
    out |= {a + b[1] + b[0] + b[2:] for a, b in sp if len(b) > 1}
    out |= {a + c + b[1:] for a, b in sp if b for c in LETTERS}
    out |= {a + c + b for a, b in sp for c in LETTERS}
    out.discard(w)
    return out


# UK -> US spellings. Unknown words that map onto a known word are valid UK spelling.
UK_US_WORDS = {
    "grey": "gray", "tyre": "tire", "tyres": "tires", "plough": "plow", "cheque": "check",
    "cheques": "checks", "aluminium": "aluminum", "kerb": "curb", "jewellery": "jewelry",
    "judgement": "judgment", "ageing": "aging", "acknowledgement": "acknowledgment",
    "sceptic": "skeptic", "sceptical": "skeptical", "programme": "program",
    "programmes": "programs", "catalogue": "catalog", "dialogue": "dialog",
    "centre": "center", "centres": "centers", "metre": "meter", "metres": "meters",
    "litre": "liter", "litres": "liters", "theatre": "theater", "fibre": "fiber",
    "defence": "defense", "offence": "offense", "licence": "license", "pretence": "pretense",
    "mum": "mom", "pyjamas": "pajamas", "storey": "story", "draught": "draft",
    "aeroplane": "airplane", "manoeuvre": "maneuver", "moustache": "mustache",
    "enrol": "enroll", "enrolment": "enrollment", "fulfil": "fulfill",
    "fulfilment": "fulfillment", "instalment": "installment", "skilful": "skillful",
    "wilful": "willful", "artefact": "artifact", "travelled": "traveled",
    "travelling": "traveling", "traveller": "traveler", "travellers": "travelers",
    "cancelled": "canceled", "cancelling": "canceling", "labelled": "labeled",
    "labelling": "labeling", "modelled": "modeled", "modelling": "modeling",
    "levelled": "leveled", "levelling": "leveling", "fuelled": "fueled",
    "counselled": "counseled", "counselling": "counseling", "signalled": "signaled",
    "signalling": "signaling", "totalled": "totaled", "marvellous": "marvelous",
}
OUR_STEMS = ("color", "favor", "honor", "labor", "neighbor", "behavior", "flavor", "harbor",
             "humor", "rumor", "vapor", "savor", "odor", "endeavor", "armor", "candor",
             "clamor", "glamor", "parlor", "splendor", "valor", "vigor", "rigor", "tumor")
OUR_ENDS = ("", "s", "ed", "ing", "ful", "less", "able", "ably", "ite", "ites", "hood",
            "hoods", "ist", "ists", "ous", "ously", "ize", "ized", "ally")
UK_US_SUFFIX = [("isation", "ization"), ("isations", "izations"), ("ise", "ize"),
                ("ised", "ized"), ("ises", "izes"), ("ising", "izing"), ("iser", "izer"),
                ("ysis", "ysis"), ("yse", "yze"), ("ysed", "yzed"), ("yses", "yzes"),
                ("ysing", "yzing"), ("tre", "ter"), ("tres", "ters")]
NOT_IZE = ("size", "prize", "seize", "maize", "sized", "sizes", "sizing")


def us_form_of_uk(lw: str) -> Optional[str]:
    """US spelling candidate for a (possibly) UK spelling, or None."""
    if lw in UK_US_WORDS:
        return UK_US_WORDS[lw]
    if "our" in lw:
        c = lw.replace("our", "or", 1)
        if any(c.startswith(s) and c[len(s):] in OUR_ENDS for s in OUR_STEMS):
            return c
    for uk, us in UK_US_SUFFIX:
        if uk != us and lw.endswith(uk) and len(lw) > len(uk) + 2:
            return lw[:-len(uk)] + us
    return None


def uk_form_of_us(lw: str) -> Optional[str]:
    """UK spelling for a US spelling (only for safe, well-known patterns)."""
    for uk, us in UK_US_WORDS.items():
        if us == lw and uk not in ("mum", "storey", "draught", "plough"):
            return uk
    for s in OUR_STEMS:
        if lw.startswith(s) and lw[len(s):] in OUR_ENDS:
            return lw.replace("or", "our", 1) if s in lw else None
    for uk, us in UK_US_SUFFIX:
        if uk != us and us.startswith(("iz", "yz")) and lw.endswith(us) \
                and len(lw) > len(us) + 3 and not lw.endswith(NOT_IZE) and lw != "prize":
            return lw[:-len(us)] + uk
    return None


class Lexicon:
    """Word -> frequency, plus personal words. Thread-safe for reads."""

    def __init__(self):
        self.freq: dict = {}
        self.user: set = set()
        self.full = False                 # a real, large dictionary is loaded
        self.source = "built-in core list (limited)"
        self._cache: dict = {}
        for f, blob in _CORE_TIERS:
            for w in blob.split():
                self.freq[w] = max(self.freq.get(w, 0), f)

    def load_big(self) -> None:
        """pyspellchecker corpus, else the system word list."""
        try:
            from spellchecker import SpellChecker
            sc = SpellChecker()
            d = getattr(sc.word_frequency, "dictionary", None)
            items = d.items() if d is not None else sc.word_frequency.items()
            n = 0
            for w, f in items:
                if w.isalpha() and w.isascii() and len(w) < 25:
                    if f > self.freq.get(w, 0):
                        self.freq[w] = int(f)
                    n += 1
            if n > 20000:
                self.full, self.source = True, f"pyspellchecker ({n:,} words)"
                self._cache.clear()
                return
        except Exception as ex:
            dbg("pyspellchecker unavailable:", ex)
        for p in ("/usr/share/dict/words", "/usr/share/dict/american-english",
                  "/usr/dict/words"):
            try:
                n = 0
                with open(p, encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        w = line.strip()
                        if w.isalpha() and w.isascii() and w == w.lower() and len(w) < 25:
                            self.freq.setdefault(w, 1)
                            n += 1
                if n > 20000:
                    self.full, self.source = True, f"system dictionary ({n:,} words)"
                    self._cache.clear()
                    return
            except OSError:
                continue

    def set_user_words(self, words) -> None:
        self.user = set(words)
        self._cache.clear()

    def known(self, lw: str) -> bool:
        return lw in self.freq or lw in self.user

    def f(self, lw: str) -> int:
        return self.freq.get(lw, 1 if lw in self.user else 0)

    # ---- suggestions
    def suggest(self, word: str, limit: int = 4, deadline: float = 0.0):
        """[(candidate, score)] best first. Lower score = better. Cached."""
        lw = word.lower().replace("\u2019", "'")
        hit = self._cache.get(lw)
        if hit is None:
            complete = [True]
            hit = self._suggest(lw, deadline, complete)
            if complete[0]:                              # incomplete (out of time) results are retried later
                if len(self._cache) > 3000:
                    self._cache.clear()
                self._cache[lw] = hit
        out, seen = [], set()
        for c, sc in hit:
            cc = match_case(word, c)
            if cc not in seen:
                seen.add(cc)
                out.append((cc, sc))
            if len(out) >= limit:
                break
        return out

    def _score(self, lw: str, c: str, d: int) -> float:
        cost = float(d)
        if d == 1 and len(lw) == len(c):
            i = next((k for k in range(len(lw)) if lw[k] != c[k]), None)
            if i is not None and c[i] in KEY_ADJ.get(lw[i], ()):
                cost -= 0.3
        if c[:1] != lw[:1]:
            cost += 0.5
        f = self.f(c)
        return cost * 2.0 - math.log10(1 + f) * 0.5

    def _suggest(self, lw: str, deadline: float = 0.0, complete=None):
        if not lw.isalpha() or len(lw) > 24:
            return []
        scored: dict = {}
        tm = TYPO_MAP.get(lw)
        if tm:
            scored[tm] = -100.0
        if True:
            e1 = edits1(lw)
            k1 = [c for c in e1 if self.known(c)]
            for c in k1:
                scored.setdefault(c, self._score(lw, c, 1))
            need2 = (not k1 or max(self.f(c) for c in k1) < 1000) and len(lw) >= 4 and len(lw) <= 13
            if need2 and deadline and time.time() > deadline:
                need2 = False
                if complete is not None:
                    complete[0] = False
            if need2:
                seen = set(e1)
                for e in e1:
                    for c in edits1(e):
                        if c not in seen and c != lw:
                            seen.add(c)
                            if self.known(c):
                                scored.setdefault(c, self._score(lw, c, 2))
        for i in range(1, len(lw)):                     # "thankyou" -> "thank you"
            a, b = lw[:i], lw[i:]
            if (len(a) >= 3 or a in SHORT_WORDS) and (len(b) >= 3 or b in SHORT_WORDS) \
                    and self.known(a) and self.known(b):
                f = min(self.f(a), self.f(b))
                scored.setdefault(f"{a} {b}", 3.2 - math.log10(1 + f) * 0.5)
        return sorted(scored.items(), key=lambda kv: kv[1])


LEX = Lexicon()


# --------------------------------------------------------------------------
# User dictionaries (folder of word lists, hot-reloaded)
# --------------------------------------------------------------------------
MY_WORDS_TEMPLATE = """\
# Writing Assistant - your personal dictionary
# One word per line. Lines starting with # are ignored.
# Anything here is treated as correctly spelled (names, jargon, slang...).
# Drop other word lists (.txt / .dic / .csv) into this folder: they all load
# automatically, no restart needed.
"""


def ensure_dictionary_folder() -> None:
    try:
        DICT_DIR.mkdir(parents=True, exist_ok=True)
        if not MY_WORDS.exists():
            MY_WORDS.write_text(MY_WORDS_TEMPLATE, encoding="utf-8")
    except Exception as ex:
        print("[wa] could not create dictionary folder:", ex)


def parse_dictionary_file(path) -> set:
    """Words from .txt/.csv/.dic; tolerates comments, 'word count' and Hunspell 'word/FLAGS'."""
    words = set()
    try:
        lines = Path(path).read_text(encoding="utf-8-sig", errors="ignore").splitlines()
    except Exception:
        return words
    for n, line in enumerate(lines):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tok = re.split(r"[\s,;\t]+", line)[0].split("/")[0].strip("\"'")
        if n == 0 and tok.isdigit():
            continue
        if tok and not tok.isdigit() and len(tok) < 60:
            words.add(tok.lower().replace("\u2019", "'"))
    return words


def dictionary_signature():
    try:
        return tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size)
                            for p in DICT_DIR.iterdir() if p.suffix.lower() in DICT_EXTS))
    except Exception:
        return ()


def load_all_dictionaries() -> set:
    words = set()
    try:
        for p in sorted(DICT_DIR.iterdir()):
            if p.suffix.lower() in DICT_EXTS:
                words |= parse_dictionary_file(p)
    except Exception:
        pass
    return words


IGNORED_FILE = HOME_DIR / "ignored.txt"


def load_ignored() -> list:
    """Issues you chose to ignore (one 'rule|text' per line); delete the file to reset."""
    try:
        return [l.strip() for l in IGNORED_FILE.read_text(encoding="utf-8").splitlines() if "|" in l][:5000]
    except Exception:
        return []


def save_ignored(tag: str) -> None:
    try:
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        with open(IGNORED_FILE, "a", encoding="utf-8") as f:
            f.write(tag.replace("\n", " ") + "\n")
    except Exception as ex:
        dbg("could not save ignore:", ex)


def import_dictionaries(paths) -> None:
    import shutil
    ensure_dictionary_folder()
    for src in paths:
        src = Path(src).expanduser()
        if not src.is_file():
            print(f"  skipped (not a file): {src}")
            continue
        dst = DICT_DIR / src.name
        if dst.exists() and dst.resolve() != src.resolve():
            dst = DICT_DIR / f"{src.stem}_{int(time.time())}{src.suffix}"
        if dst.resolve() != src.resolve():
            shutil.copy2(src, dst)
        print(f"  imported {src.name}: {len(parse_dictionary_file(dst))} words -> {dst}")


def open_folder(path) -> None:
    try:
        if WIN:
            os.startfile(str(path))                      # type: ignore[attr-defined]
        elif MAC:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as ex:
        print("[wa] could not open folder:", ex)

# --------------------------------------------------------------------------
# Rule engine: check_text() is a pure function of the text.
# --------------------------------------------------------------------------
KIND_PRIORITY = {"spelling": 0, "grammar": 1, "punct": 2, "style": 3}


@dataclass
class Issue:
    key: str
    kind: str                 # spelling | grammar | punct | style
    rule: str
    message: str
    s: int                    # edit span [s, e) that suggestions replace
    e: int
    hs: int                   # highlight span
    he: int
    word: str                 # highlighted text
    sugg: list = field(default_factory=list)
    auto: bool = False        # safe to apply in "Fix all"
    rect: Optional[tuple] = None
    origin: tuple = (0, 0)


CONTRACTIONS = {
    "dont": "don't", "cant": "can't", "isnt": "isn't", "doesnt": "doesn't",
    "didnt": "didn't", "wasnt": "wasn't", "couldnt": "couldn't", "shouldnt": "shouldn't",
    "wouldnt": "wouldn't", "havent": "haven't", "hasnt": "hasn't", "arent": "aren't",
    "werent": "weren't", "im": "I'm", "ive": "I've", "youre": "you're", "youve": "you've",
    "thats": "that's", "theres": "there's", "theyre": "they're", "theyve": "they've",
    "whats": "what's", "hadnt": "hadn't", "wouldve": "would've", "shouldve": "should've",
    "couldve": "could've", "mustnt": "mustn't",
}
CONTRACTION_SUFFIXES = {"s", "re", "ve", "ll", "d", "m", "t"}
SLANG_OK = {
    "gonna", "wanna", "gotta", "kinda", "sorta", "ok", "okay", "lol", "omg", "btw", "thx",
    "pls", "plz", "yeah", "yep", "nope", "hey", "hmm", "umm", "wow", "tbh", "imo", "fyi",
    "asap", "vs", "etc", "wifi", "email", "emails", "online", "app", "apps", "iphone",
    "android", "youtube", "gmail", "google", "ya", "yo", "haha", "hahaha", "ugh", "aw",
    "ah", "oh", "uh", "um", "eh", "ha", "hi", "bye", "thanks", "cheers",
}
ABBREV = {"e.g", "i.e", "etc", "dr", "mr", "mrs", "ms", "prof", "vs", "inc", "ltd", "jr",
          "sr", "st", "no", "approx", "fig", "cf", "al", "co", "corp", "dept", "est", "vol"}
DUP_OK = {"had", "that", "very", "so", "really", "can", "no", "bye", "ha", "yeah", "go",
          "night", "knock", "blah", "bla", "tut", "yes", "now", "hey", "oh", "ah", "is",
          "do", "bang", "chop", "zzz"}
DAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
MONTHS = {"january", "february", "april", "june", "july", "september", "october",
          "november", "december"}                     # may/march/august are also ordinary words
SPECIAL = set("@/\\_#<>{}=+*~^|$%&`")
WORD_RE = re.compile(r"[A-Za-z]+(?:['\u2019][A-Za-z]+)*")
CHUNK_RE = re.compile(r"\S+")
DOMAINISH = re.compile(r"[A-Za-z0-9]\.[a-z]{2,4}(?:\b|/)")

CONS_SOUND_VOWEL_LETTER = ("one", "once", "eu", "ewe", "use", "usu", "usa", "uti", "ubiq", "uk", "ura",
                           "unanim", "urin", "uku")
VOWEL_SOUND_H = {"hour", "hours", "hourly", "honest", "honestly", "honor", "honour", "honors",
                 "honorable", "honourable", "honorary", "heir", "heirs", "heiress"}
CAUSATIVE = {"let", "lets", "make", "makes", "made", "help", "helps", "helped", "watch",
             "watched", "see", "saw", "hear", "heard", "have", "had", "get", "got", "want",
             "wanted", "need", "needed", "tell", "told", "ask", "asked", "expect", "expected",
             "force", "forced", "allow", "allowed", "enable", "enables", "cause", "causes",
             "keep", "kept", "leave", "left", "to", "that", "if", "lest", "suggest", "suggested",
             "demand", "demanded", "insist", "insisted", "recommend", "recommended", "require",
             "required", "propose", "proposed", "order", "ordered", "request", "requested",
             "do", "does", "did", "can", "could", "will", "would", "shall", "should", "may",
             "might", "must", "has", "is", "was", "are", "were", "am", "be", "been",
             "dont", "don't", "doesn't", "didn't", "won't", "can't", "couldn't", "wouldn't",
             "shouldn't", "how", "what", "why", "where", "when", "who", "whom",
             "feel", "felt", "notice", "noticed", "imagine", "prefer", "like", "liked", "love",
             "loved", "hate", "wish", "bet", "stop", "prevent", "find", "found"}
HAVE_FORMS = {"have", "has", "had", "haven't", "hasn't", "hadn't"}

PARTICIPLES = {
    "went": "gone", "did": "done", "saw": "seen", "ate": "eaten", "took": "taken",
    "gave": "given", "spoke": "spoken", "wrote": "written", "rode": "ridden",
    "drove": "driven", "froze": "frozen", "chose": "chosen", "began": "begun",
    "rang": "rung", "sang": "sung", "swam": "swum", "ran": "run", "came": "come",
    "flew": "flown", "grew": "grown", "knew": "known", "threw": "thrown", "drew": "drawn",
    "shook": "shaken", "stole": "stolen", "wore": "worn", "tore": "torn", "blew": "blown",
    "woke": "woken", "drank": "drunk", "swore": "sworn", "broke": "broken",
}
PAST_TO_BASE = {
    "went": "go", "ate": "eat", "took": "take", "gave": "give", "came": "come", "ran": "run",
    "wrote": "write", "knew": "know", "spoke": "speak", "chose": "choose", "began": "begin",
    "drove": "drive", "flew": "fly", "grew": "grow", "threw": "throw", "wore": "wear",
    "made": "make", "said": "say", "got": "get", "felt": "feel", "told": "tell",
    "bought": "buy", "brought": "bring", "caught": "catch", "taught": "teach",
    "spent": "spend", "stood": "stand", "kept": "keep", "met": "meet", "heard": "hear",
    "left": "leave", "had": "have", "saw": "see", "broke": "break",
}
PAST_AFTER_MODAL = ("went", "ate", "took", "gave", "came", "ran", "wrote", "knew", "spoke",
                    "chose", "began", "drove", "flew", "grew", "threw", "wore", "made")
S_VERBS = ("have do go say make take know get want see think come give feel need try use "
           "bring happen write provide seem show tell work call ask play start help live "
           "believe hold turn move mean keep begin run learn change follow stop speak create lose pay meet").split()


def s_form(v: str) -> str:
    irregular = {"have": "has", "do": "does", "go": "goes"}
    if v in irregular:
        return irregular[v]
    if v.endswith(("s", "x", "z", "ch", "sh", "o")):
        return v + "es"
    if v.endswith("y") and v[-2:-1] not in "aeiou":
        return v[:-1] + "ies"
    return v + "s"


S_FORM_TO_BASE = {s_form(v): v for v in S_VERBS}
MODAL_S_VERBS = ("goes does has says gets knows thinks comes gives takes writes brings "
                 "happens provides follows learns speaks creates loses pays meets").split()

# ---- phrase rules -----------------------------------------------------------
SPR = r"[ \t]+"
RULES: list = []      # (rule_id, kind, regex, repl, message, auto, guard)


def R(rid, kind, pat, repl, msg, auto=True, guard=None, flags=re.I):
    RULES.append((rid, kind, re.compile(pat.replace("<S>", SPR), flags), repl, msg, auto, guard))


def _prev_word(text: str, i: int) -> str:
    m = re.search(r"([A-Za-z'\u2019]+)[ \t]*$", text[max(0, i - 40):i])
    return m.group(1).lower().replace("\u2019", "'") if m else ""


def _guard_subject_verb(m, text):
    subj = m.group(1).lower()
    prev = _prev_word(text, m.start())
    if prev in CAUSATIVE:
        return False
    return True


def _repl_sv(m):
    return f"{m.group(1)} {s_form(m.group(2).lower())}"


def _repl_participle(m):
    return f"{m.group(1)} {PARTICIPLES[m.group(2).lower()]}"


def _repl_past_base(m):
    return f"{m.group(1)} {PAST_TO_BASE[m.group(2).lower()]}"


def _guard_seen(m, text):
    return _prev_word(text, m.start()) not in HAVE_FORMS


NOT_COMP = r"(?![-'\u2019\w])"
R("modal-of", "grammar", r"\b(should|could|would|must|might|couldn't|shouldn't|wouldn't|mustn't)<S>of\b",
  r"\1 have", "“{old}” should be “{new}”")
R("your-youre", "grammar",
  r"\b(your)<S>(welcome|going<S>to|not|a|an|the|so|always|never|really|already)\b" + NOT_COMP,
  r"you're \2", "“{old}” → “{new}” (you are)")
R("your-youre2", "grammar", r"\b(your)<S>(right|wrong|correct)(?=[ \t]*[.,!?]|[ \t]+(?:about|that|and|but)\b|[ \t]*$)",
  r"you're \2", "“{old}” → “{new}” (you are)")
R("its-itis", "grammar",
  r"\b(its)<S>(a|an|been|not|going<S>to|just|so|too|really|always|never|also|still|the|like|because|what)\b" + NOT_COMP,
  r"it's \2", "“{old}” → “{new}” (it is / it has)")
R("itis-its", "grammar", r"\b(it's|it\u2019s)<S>own\b", "its own", "“{old}” → “{new}” (possessive)")
R("their-there", "grammar",
  r"\b(their)<S>(are|is|was|were|isn't|aren't|wasn't|weren't)\b", r"there \2", "“{old}” → “{new}”")
R("their-theyre", "grammar", r"\b(their|there)<S>(going<S>to|gonna)\b", r"they're \2", "“{old}” → “{new}” (they are)")
R("their-theyre2", "grammar", r"\b(their)<S>not\b", "they're not", "“{old}” → “{new}” (they are)")
R("theyre-their", "grammar", r"\b(they're|they\u2019re|there)<S>own\b", "their own", "“{old}” → “{new}” (possessive)")
R("then-than", "grammar",
  r"\b(more|less|better|worse|rather|other|bigger|smaller|greater|fewer|higher|lower|faster|slower|"
  r"easier|harder|older|younger|taller|longer|larger|stronger|cheaper|smarter)<S>then\b", r"\1 than",
  "“{old}” → “{new}” (comparison)")
R("and-then", "grammar", r"\b(and)<S>than\b", r"\1 then", "“{old}” → “{new}” (sequence)")
R("to-too", "grammar",
  r"\b(is|was|are|were|be|am|it's|that's|isn't|wasn't|seems|looks|feels|getting|gets|get)<S>to<S>"
  r"(late|early|soon|big|small|hard|easy|old|young|expensive|cold|hot|much|many|few|little|"
  r"far|loud|bright|heavy|bad|good|busy|tired|risky|dangerous|difficult|complicated|high|low)\b" + NOT_COMP,
  r"\1 too \2", "“{old}” → “{new}” (excessively)")
R("too-to", "grammar",
  r"\b(going|want|wanted|need|needed|have|had|able|trying|try|tried|like|love)<S>too<S>"
  r"(be|do|go|see|get|make|take|have|say|know|find|give)\b", r"\1 to \2", "“{old}” → “{new}”")
R("affect-noun", "grammar",
  r"\b(an?|the|this|that|no|any|side|negative|positive|major|big|huge|great|significant|lasting|profound)<S>affect\b" + NOT_COMP,
  r"\1 effect", "“{old}” → “{new}” (the noun is “effect”)")
R("effect-verb", "grammar",
  r"\b(greatly|negatively|positively|adversely|deeply|seriously|strongly|badly|directly)<S>effect(s|ed|ing)?\b",
  r"\1 affect\2", "“{old}” → “{new}” (the verb is “affect”)")
R("effect-verb2", "grammar",
  r"\b(will|would|could|can|may|might|to|not|didn't|doesn't|don't)<S>effect(s|ed|ing)?<S>"
  r"(my|your|his|her|our|their|us|me|you|him|them|how|whether)\b",
  r"\1 affect\2 \3", "“{old}” → “{new}” (the verb is “affect”)")
R("loose-lose", "grammar",
  r"\b(to|will|would|could|can|may|might|don't|didn't|won't|cannot|can't|couldn't|wouldn't|shouldn't|should|must|not|gonna)<S>loose\b" + NOT_COMP,
  r"\1 lose", "“{old}” → “{new}” (to lose)")
R("advise-advice", "grammar",
  r"\b(an?|the|some|good|bad|my|your|his|her|any|no|more|this|that|legal|medical|expert|professional|great|free|sound|financial)<S>advise\b" + NOT_COMP,
  r"\1 advice", "“{old}” → “{new}” (noun)")
R("advice-advise", "grammar",
  r"\b(to|will|would|can|could|should|may|might|please|we|I|they)<S>advice(?=[ \t]+(?:you|me|him|her|us|them|that|against)\b)",
  r"\1 advise", "“{old}” → “{new}” (verb)")
R("whos-whose", "grammar",
  r"\b(who's|who\u2019s)<S>(car|book|house|idea|fault|turn|name|phone|job|side|bag|dog|cat|child|children|bike)\b",
  "whose \\2", "“{old}” → “{new}” (possessive)")
R("whose-whos", "grammar",
  r"\b(whose)<S>(going|coming|been|got|not|there|here|the|a|an|doing|having)\b", r"who's \2",
  "“{old}” → “{new}” (who is)")
R("whether-weather", "grammar",
  r"\b(the|nice|bad|good|great|cold|hot|warm|terrible|awful|lovely|beautiful)<S>whether\b(?![-\w])(?!<S>(?:or|to)\b)",
  r"\1 weather", "“{old}” → “{new}”")
R("weather-whether", "grammar", r"\bweather<S>(or<S>not|to)\b", r"whether \1", "“{old}” → “{new}”")
R("except-accept", "grammar",
  r"\b(I|we|they|you|to|will|would|can|could|should|not|don't|didn't|won't|cannot|please)<S>except<S>"
  r"(the|a|an|my|your|this|that|our|his|her|their|it|all|any|him|them|us|me|responsibility|payment|apology|offer|invitation|gift|defeat)\b",
  r"\1 accept \2", "“{old}” → “{new}”")
R("between-i", "grammar", r"\bbetween<S>(you|him|her|them|us)<S>and<S>I\b", r"between \1 and me",
  "“{old}” → “{new}” (after a preposition)")
R("less-fewer", "grammar",
  r"\bless<S>(people|items|cars|mistakes|errors|students|words|things|calories|friends|problems|days|hours|"
  r"minutes|options|choices|customers|users|employees|children|kids|women|men|calls|emails|messages|"
  r"questions|books|pages|bugs|steps|tasks|meetings|points)\b", r"fewer \1", "“{old}” → “{new}” (countable)")
R("be-suppose", "grammar", r"\b(is|are|was|were|am|be|been)<S>(suppose|use)<S>to\b(?!-)",
  lambda m: f"{m.group(1)} {m.group(2)}d to", "“{old}” → “{new}”")
R("irregardless", "grammar", r"\birregardless\b", "regardless", "“{old}” → “{new}”")
R("supposably", "grammar", r"\bsupposably\b", "supposedly", "“{old}” → “{new}”")
R("intensive", "grammar", r"\bfor<S>all<S>intensive<S>purposes\b", "for all intents and purposes", "“{old}” → “{new}”")
R("could-care-less", "grammar", r"\bcould<S>care<S>less\b", "couldn't care less", "“{old}” → “{new}”", auto=False)
R("i-seen", "grammar", r"\b(I|we|they|he|she|you)<S>seen\b", r"\1 saw", "“{old}” → “{new}” (simple past)", guard=_guard_seen)
R("participle", "grammar",
  r"\b(have|has|had|haven't|hasn't|hadn't)<S>(" + "|".join(PARTICIPLES) + r")\b",
  _repl_participle, "“{old}” → “{new}” (past participle)")
R("passive-participle", "grammar",
  r"\b(was|were|been|being)<S>(took|gave|wrote|chose|ate|saw|spoke|stole|drove|forgot|began|drew|threw|wore|tore|froze)\b",
  lambda m: f"{m.group(1)} {({'forgot': 'forgotten', 'stole': 'stolen'}).get(m.group(2).lower(), PARTICIPLES.get(m.group(2).lower(), m.group(2)))}",
  "“{old}” → “{new}” (past participle)")
R("did-base", "grammar",
  r"\b(did|didn't)<S>(" + "|".join(PAST_TO_BASE) + r")\b", _repl_past_base,
  "“{old}” → “{new}” (base form after “did”)")
R("modal-base", "grammar",
  r"\b(could|should|would|must|might|shall|couldn't|shouldn't|wouldn't|mustn't)<S>(" + "|".join(PAST_AFTER_MODAL) + r")\b",
  _repl_past_base, "“{old}” → “{new}” (base form after a modal)")
R("modal-s", "grammar",
  r"\b(could|should|would|must|might|shall|couldn't|shouldn't|wouldn't)<S>(" + "|".join(MODAL_S_VERBS) + r")\b",
  lambda m: f"{m.group(1)} {S_FORM_TO_BASE.get(m.group(2).lower(), {'goes': 'go', 'does': 'do', 'has': 'have'}.get(m.group(2).lower(), m.group(2)))}",
  "“{old}” → “{new}” (base form after a modal)")
R("sv-plural-was", "grammar", r"\b(they|we|you)<S>was\b", r"\1 were", "“{old}” → “{new}”")
R("sv-plural-has", "grammar", r"\b(they|we|you|I)<S>has\b", r"\1 have", "“{old}” → “{new}”")
R("sv-plural-is", "grammar", r"\b(they|we|you)<S>is\b", r"\1 are", "“{old}” → “{new}”")
R("sv-i-are", "grammar", r"\b(I)<S>(are|is)\b", r"\1 am", "“{old}” → “{new}”")
R("sv-he-are", "grammar", r"\b(he|she)<S>are\b", r"\1 is", "“{old}” → “{new}”", guard=_guard_subject_verb)
R("sv-dont", "grammar", r"\b(he|she|it)<S>(don't|dont)\b", r"\1 doesn't", "“{old}” → “{new}”", guard=_guard_subject_verb)
R("sv-doesnt", "grammar", r"\b(they|we|you|I)<S>doesn't\b", r"\1 don't", "“{old}” → “{new}”")
R("sv-third", "grammar", r"\b(he|she|it)<S>(" + "|".join(S_VERBS) + r")\b" + NOT_COMP, _repl_sv,
  "“{old}” → “{new}” (verb agreement)", guard=_guard_subject_verb)

# ---- style tables -----------------------------------------------------------
WORDY = {
    "in order to": "to", "due to the fact that": "because", "at this point in time": "now",
    "in the event that": "if", "a large number of": "many", "for the purpose of": "for",
    "has the ability to": "can", "is able to": "can", "in spite of the fact that": "although",
    "with regard to": "about", "in regards to": "regarding", "in the near future": "soon",
    "at the present time": "now", "on a daily basis": "daily", "make a decision": "decide",
    "take into consideration": "consider", "give consideration to": "consider",
    "it is important to note that": "", "needless to say": "", "each and every": "each",
    "first and foremost": "first", "prior to": "before", "subsequent to": "after",
    "in the process of": "", "a number of": "several", "despite the fact that": "although",
    "until such time as": "until", "with the exception of": "except",
}
REDUNDANT = {
    "end result": "result", "past history": "history", "future plans": "plans",
    "free gift": "gift", "added bonus": "bonus", "advance planning": "planning",
    "close proximity": "proximity", "completely finished": "finished", "revert back": "revert",
    "return back": "return", "repeat again": "repeat", "unexpected surprise": "surprise",
    "join together": "join", "merge together": "merge", "combine together": "combine",
    "absolutely essential": "essential", "actual fact": "fact", "final outcome": "outcome",
    "basic fundamentals": "fundamentals", "general consensus": "consensus",
    "personal opinion": "opinion", "true fact": "fact", "exactly the same": "the same",
    "plan ahead": "plan", "sum total": "total", "very unique": "unique", "totally unique": "unique",
    "collaborate together": "collaborate", "cooperate together": "cooperate",
}
CLICHES = {
    "at the end of the day": "ultimately", "think outside the box": "think creatively",
    "low-hanging fruit": "easy wins", "circle back": "follow up", "touch base": "check in",
    "move the needle": "make a difference", "paradigm shift": "major change",
    "game changer": "breakthrough", "game-changer": "breakthrough", "deep dive": "detailed look",
    "take it to the next level": "improve it", "going forward": "from now on",
    "synergy": "cooperation", "utilize": "use", "utilise": "use", "commence": "start",
}
FILLERS = ("very really just actually basically literally totally simply quite somewhat "
           "extremely definitely honestly").split()
PASSIVE_RE = re.compile(
    r"\b(is|are|was|were|been|being|be)[ \t]+(\w+ed|\w+en|known|shown|made|given|taken|seen|done|written|"
    r"built|found|held|kept|sent|paid|told|sold)[ \t]+by\b", re.I)
LEVELS = {"off": 0, "light": 1, "standard": 2, "full": 3}


def sentence_start_at(text: str, s: int, start_ok: bool) -> bool:
    """Is the word beginning at index s the first word of a sentence?"""
    i = s - 1
    while i >= 0 and text[i] in "\"'\u201c\u2018([{*_":
        i -= 1
    if i < 0:
        return start_ok
    if text[i] == "\n":
        return True
    if not text[i].isspace():
        return False
    j = i
    while j >= 0 and text[j] in " \t":
        j -= 1
    if j < 0:
        return start_ok
    if text[j] == "\n":
        return True
    while j >= 0 and text[j] in "\"'\u201d\u2019)]}*_":
        j -= 1
    if j < 0:
        return False
    ch = text[j]
    if ch in "!?":
        return True
    if ch == ".":
        if j >= 1 and text[j - 1] == ".":
            return False                              # ellipsis: sentence continues
        k = j
        while k > 0 and (text[k - 1].isalpha() or text[k - 1] == "."):
            k -= 1
        tok = text[k:j].lower().strip(".")
        return not (tok == "" or len(tok) == 1 or tok in ABBREV)
    return False


def article_problem(a: str, nxt: str):
    """Return the right article ('a'/'an') for `a` + `nxt`, or None if fine/unsure."""
    if not nxt.isalpha() or nxt.isupper() or len(nxt) < 2:
        return None
    ln = nxt.lower()
    if ln in ("herb", "herbs", "herbal", "an", "the", "historic", "historical", "historically"):
        return None
    if ln in VOWEL_SOUND_H:
        vowel = True
    elif ln[0] in "aeiou":
        vowel = True
        if ln.startswith(CONS_SOUND_VOWEL_LETTER):
            vowel = False
        if ln.startswith("uni") and len(ln) > 3 and ln[3] not in "nmd":
            vowel = False
    else:
        vowel = False
    want = "an" if vowel else "a"
    return want if a.lower() != want else None


def check_text(text: str, *, lex: Lexicon = None, cfg: dict = None, start_is_sentence: bool = False,
               active: Optional[tuple] = None, ignored=frozenset()) -> list:
    """All issues in `text`, sorted by position. `active` = span being typed (skipped)."""
    lex = lex or LEX
    cfg = cfg or CFG
    t_budget = time.time() + 2.0                     # cap on time spent generating suggestions
    level = LEVELS.get(cfg.get("style_level", "standard"), 2)
    n = len(text)
    if not n:
        return []
    out: list = []
    mask = bytearray(n)                              # 1 = inside a URL/path/code chunk
    for m in CHUNK_RE.finditer(text):
        ch = m.group().strip(".,;:!?\"'()[]\u201c\u201d\u2018\u2019")
        if (any(c in SPECIAL or c.isdigit() for c in ch) or "://" in ch or ch.lower().startswith("www.")
                or DOMAINISH.search(ch) or re.search(r"\w\(\)", ch)):
            mask[m.start():m.end()] = b"\x01" * (m.end() - m.start())

    def masked(a, b):
        return any(mask[a:b])

    def add(kind, rule, msg, s, e, hs, he, sugg, auto=False):
        if masked(hs, max(he, hs + 1)):
            return
        word = text[hs:he]
        out.append(Issue("", kind, rule, msg, s, e, hs, he, word, list(sugg), auto))

    # ---- phrase rules
    for rid, kind, rx, repl, msg, auto, guard in RULES:
        for m in rx.finditer(text):
            if guard and not guard(m, text):
                continue
            old = m.group(0)
            new = repl(m) if callable(repl) else m.expand(repl)
            new = match_case(old, new)
            if new == old:
                continue
            add(kind, rid, msg.format(old=old, new=new), m.start(), m.end(), m.start(), m.end(), [new], auto)

    # ---- style phrases
    def phrase_pass(table, label, min_level):
        if level < min_level:
            return
        for ph, rp in table.items():
            for m in re.finditer(r"(?<![\w'-])" + re.escape(ph).replace(r"\ ", r"[ \t]+") + r"(?![\w'-])", text, re.I):
                old = m.group(0)
                if rp:
                    new = match_case(old, rp)
                    sugg, msg = [new], f"{label}: “{old}” → “{new}”"
                else:
                    msg, sugg = f"{label}: “{old}” can usually be cut", []
                    e2 = m.end() + (1 if text[m.end():m.end() + 1] == " " else 0)
                    if e2 <= n and m.end() < n and text[m.end():m.end() + 1] not in ",;":
                        sugg = [""]
                        add("style", "wordy", msg, m.start(), e2, m.start(), m.end(), sugg)
                        continue
                add("style", "wordy", msg, m.start(), m.end(), m.start(), m.end(), sugg)

    phrase_pass(WORDY, "Wordy", 1)
    phrase_pass(REDUNDANT, "Redundant", 1)
    phrase_pass(CLICHES, "Cliché/jargon", 2)
    if level >= 3:
        for m in PASSIVE_RE.finditer(text):
            add("style", "passive", "Passive voice — consider an active sentence", m.start(), m.end(),
                m.start(), m.end(), [])

    # ---- punctuation / spacing
    for m in re.finditer(r"(?<=[^\s.!?:;])( {2,})(?=\S)", text):
        add("punct", "double-space", "Extra space", m.start(), m.end(), m.start(), m.end(), [" "], True)
    for m in re.finditer(r"(?<=[A-Za-z0-9)\"\u201d\u2019'])([ \t]+)(?=,|[.!?](?![.!?\w]))", text):
        add("punct", "space-before-punct", "Space before punctuation", m.start(), m.end(),
            m.start(), m.end(), [""], True)
    for m in re.finditer(r",{2,}", text):
        add("punct", "repeat-comma", "Repeated comma", m.start(), m.end(), m.start(), m.end(), [","], True)
    for m in re.finditer(r"(?<!\.)\.\.(?!\.)", text):
        add("punct", "double-period", "Two periods — use one, or three for an ellipsis",
            m.start(), m.end(), m.start(), m.end(), [".", "\u2026"], False)
    for rid, pat in (("missing-space-comma", r"(?<=[A-Za-z ]),(?=[A-Za-z])"),
                     ("missing-space-stop", r"(?<=[a-z]{2})\.(?=[A-Z][a-z])"),
                     ("missing-space-mark", r"(?<=[A-Za-z])[!?;](?=[A-Za-z])")):
        for m in re.finditer(pat, text):
            add("punct", rid, f"Missing space after “{m.group()}”", m.end(), m.end(), m.start(), m.end() + 1,
                [" "], True)

    # ---- words
    words = [(m.start(), m.end(), m.group()) for m in WORD_RE.finditer(text)]
    counts = Counter(w.lower().replace("\u2019", "'") for _, _, w in words)
    prev = None
    for idx, (s, e, w) in enumerate(words):
        lw = w.lower().replace("\u2019", "'")
        if masked(s, e):
            prev = None
            continue
        gap = text[prev[1]:s] if prev else None
        pw = prev[2] if prev else ""
        user_ok = lw in lex.user
        # a / an
        if prev and gap and gap.strip(" \t") == "" and pw.lower() in ("a", "an") and not user_ok:
            want = article_problem(pw, w)
            if want:
                add("grammar", "article", f"Use “{want}” before “{w}”", prev[0], prev[1], prev[0], prev[1],
                    [match_case(pw, want)], True)
        # repeated word
        if prev and gap and gap.strip(" \t") == "" and gap and lw == pw.lower().replace("\u2019", "'") \
                and lw.isalpha() and len(lw) > 1 and lw not in DUP_OK:
            add("grammar", "repeat", "Repeated word", prev[0], e, prev[0], e, [pw], True)
        prev = (s, e, w)
        if user_ok:
            continue
        # contractions without apostrophe
        if lw in CONTRACTIONS:
            fix = match_case(w, CONTRACTIONS[lw])
            if sentence_start_at(text, s, start_is_sentence) and fix[:1].islower():
                fix = fix[0].upper() + fix[1:]
            add("grammar", "apostrophe", "Missing apostrophe", s, e, s, e, [fix], True)
            continue
        if w == "i" and text[s - 1:s] != "(" and text[e:e + 1] not in (")", "."):
            add("grammar", "cap-i", "Capitalize “I”", s, e, s, e, ["I"], True)
            continue
        if w[0] == "i" and lw in ("i'm", "i've", "i'll", "i'd"):
            add("grammar", "cap-i", "Capitalize “I”", s, e, s, e, ["I" + w[1:]], True)
            continue
        if lw in DAYS | MONTHS and w == lw:
            add("grammar", "cap-name", "Capitalize days and months", s, e, s, e, [w.capitalize()], True)
            continue
        sent_start = sentence_start_at(text, s, start_is_sentence)
        spell = _spelling_issue(w, lw, s, e, text, lex, cfg, sent_start, counts[lw], t_budget)
        if spell:
            out.append(spell)
            continue
        if sent_start and w == lw and w.isalpha() and lw not in SLANG_OK and not (len(w) == 1 and w != "a"):
            add("grammar", "cap-sentence", "Capitalize the first word of a sentence", s, e, s, e,
                [w[:1].upper() + w[1:]], True)
        elif level >= 2 and lw in FILLERS:
            e2 = e + (1 if text[e:e + 1] == " " else 0)
            if e < n and text[e:e + 1] in ",;.!?":
                sg = []
            elif e2 == e and s > 0 and text[s - 1] == " ":
                sg = [""]
                add("style", "filler", f"Filler word: “{w}” — usually safe to cut", s - 1, e, s, e, sg)
                continue
            else:
                sg = [""]
            add("style", "filler", f"Filler word: “{w}” — usually safe to cut", s, e2, s, e, sg)

    # ---- finalize: ignore, active word, overlaps, keys
    res = []
    for it in out:
        if f"{it.rule}|{it.word.lower()}" in ignored:
            continue
        if active and it.hs < active[1] and it.he > active[0]:
            continue
        res.append(it)
    res.sort(key=lambda i: (KIND_PRIORITY[i.kind], i.hs, -(i.he - i.hs)))
    kept: list = []
    for it in res:
        if any(it.hs < k.he and it.he > k.hs for k in kept):
            continue
        kept.append(it)
    kept.sort(key=lambda i: (i.hs, i.he))
    seen: dict = {}
    for it in kept:
        base = f"{it.rule}|{it.word.lower()}"
        seen[base] = seen.get(base, 0) + 1
        it.key = f"{base}|{seen[base]}"
    return kept


def _spelling_issue(w, lw, s, e, text, lex: Lexicon, cfg: dict, sent_start: bool, repeats: int = 1,
                    deadline: float = 0.0):
    pref = cfg.get("spelling", "any")

    def mk(msg, sugg, auto):
        if sent_start and w[:1].islower():           # fix the spelling AND the capital in one go
            sugg = [x[:1].upper() + x[1:] for x in sugg]
        return Issue("", "spelling", "spell", msg, s, e, s, e, w, sugg, auto)

    if lw in TYPO_MAP:
        fix = match_case(w, TYPO_MAP[lw])
        return mk("Common misspelling", [fix] + [c for c, _ in lex.suggest(w, 3, deadline) if c != fix][:2], True)
    if "'" in lw:
        return None                                  # contractions/possessives: leave alone
    if len(lw) < 2 or w.isupper() or any(c.isupper() for c in w[1:]) or re.search(r"(.)\1\1", lw):
        return None
    if text[max(0, s - 1):s] == "-" or text[e:e + 1] == "-":
        if len(lw) < 4:
            return None                              # compound fragment such as "re-", "co-"
    if pref == "us":
        us = us_form_of_uk(lw)
        if us and lex.known(us):
            return mk("UK spelling — your setting prefers US", [match_case(w, us)], True)
    if lex.known(lw):
        if pref == "uk":
            uk = uk_form_of_us(lw)
            if uk:
                return mk("US spelling — your setting prefers UK/AU", [match_case(w, uk)], True)
        return None
    us = us_form_of_uk(lw)
    if us and lex.known(us):
        return None                                  # valid UK/AU spelling
    if repeats >= 3:
        return None                                  # used again and again: a name or jargon, not a typo
    if not lex.full:
        return None                                  # tiny list: only the curated typo table is trusted
    if w[0].isupper() and not sent_start:
        return None                                  # probably a name
    sg = lex.suggest(w, 4, deadline)
    if not sg:
        return None
    best = sg[0]
    auto = edit_distance(lw, best[0].lower()) <= 1 and (len(sg) == 1 or sg[1][1] - best[1] >= 1.0)
    return mk("Possible misspelling", [c for c, _ in sg][:3], auto)


# --------------------------------------------------------------------------
# Platform helpers (everything best-effort: failure must never crash the app)
# --------------------------------------------------------------------------
import ctypes

if WIN:
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                    wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                    ("rcCaret", wintypes.RECT)]


def make_dpi_aware() -> None:
    if WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def win_style(win, click_through: bool = False) -> None:
    """Windows: make a Tk window non-activating so clicking it never steals focus."""
    if not WIN:
        return
    try:
        win.update_idletasks()
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        ex = user32.GetWindowLongW(hwnd, -20)
        ex |= 0x08000000 | 0x00000080                # NOACTIVATE | TOOLWINDOW
        if click_through:
            ex |= 0x00000020 | 0x00080000            # TRANSPARENT | LAYERED
        user32.SetWindowLongW(hwnd, -20, ex)
    except Exception:
        pass


@dataclass
class Target:
    wid: object = None
    pid: Optional[int] = None
    name: str = ""


def foreground() -> Target:
    """The active window: (id, pid, lower-case process/app name). Best effort."""
    try:
        if WIN:
            hwnd = user32.GetForegroundWindow()
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            name = ""
            h = kernel32.OpenProcess(0x1000, False, pid.value)
            if h:
                try:
                    buf = ctypes.create_unicode_buffer(520)
                    size = wintypes.DWORD(520)
                    if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        name = Path(buf.value).stem.lower()
                finally:
                    kernel32.CloseHandle(h)
            return Target(hwnd, pid.value, name)
        if MAC:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return Target(app.processIdentifier(), app.processIdentifier(),
                          str(app.localizedName() or "").lower())
        out = subprocess.check_output(["xdotool", "getactivewindow"], timeout=0.4,
                                      stderr=subprocess.DEVNULL).decode().strip()
        pid, name = None, ""
        try:
            pid = int(subprocess.check_output(["xdotool", "getwindowpid", out], timeout=0.4,
                                              stderr=subprocess.DEVNULL).decode().strip())
            name = Path(f"/proc/{pid}/comm").read_text().strip().lower()
        except Exception:
            pass
        return Target(out, pid, name)
    except Exception:
        return Target()


def activate(t: Target) -> bool:
    """Give focus back to a previously active window (used after clicking our own card)."""
    try:
        if t.wid is None:
            return False
        if WIN:
            return bool(user32.SetForegroundWindow(t.wid))
        if MAC:
            from AppKit import NSRunningApplication
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(t.pid)
            return bool(app and app.activateWithOptions_(2))
        subprocess.check_call(["xdotool", "windowactivate", str(t.wid)], timeout=0.6,
                              stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def app_is_ignored(name: str, ignore) -> bool:
    n = (name or "").lower().replace(".exe", "")
    if not n:
        return False
    for ig in ignore:
        ig = str(ig).lower().replace(".exe", "")
        if n == ig or (len(ig) >= 6 and ig in n):
            return True
    return False


def window_origin():
    """Top-left of the active window (Windows) so marks can follow window moves."""
    if WIN:
        try:
            r = wintypes.RECT()
            user32.GetWindowRect(user32.GetForegroundWindow(), ctypes.byref(r))
            return (r.left, r.top)
        except Exception:
            pass
    return (0, 0)


class CaretLocator:
    """Screen position of the text caret as (x, y_top, height) or None (Windows & macOS)."""

    def __init__(self):
        self.errors = 0

    def get(self):
        if self.errors > 40 or not (WIN or MAC):
            return None
        try:
            r = self._win() if WIN else self._mac()
            self.errors = 0
            return r
        except Exception:
            self.errors += 1
            return None

    @staticmethod
    def _win():
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetGUIThreadInfo(0, ctypes.byref(info)) or not info.hwndCaret:
            return None
        rc = info.rcCaret
        if rc.left == rc.right and rc.top == rc.bottom:
            return None
        pt = wintypes.POINT(rc.left, rc.top)
        user32.ClientToScreen(info.hwndCaret, ctypes.byref(pt))
        return (pt.x, pt.y, max(rc.bottom - rc.top, 12))

    @staticmethod
    def _mac():
        from ApplicationServices import (
            AXUIElementCreateSystemWide, AXUIElementCopyAttributeValue,
            AXUIElementCopyParameterizedAttributeValue, AXValueGetValue, AXValueCreate,
            kAXFocusedUIElementAttribute, kAXSelectedTextRangeAttribute,
            kAXBoundsForRangeParameterizedAttribute, kAXValueCFRangeType, kAXValueCGRectType)
        from CoreFoundation import CFRangeMake
        err, el = AXUIElementCopyAttributeValue(AXUIElementCreateSystemWide(), kAXFocusedUIElementAttribute, None)
        if err or el is None:
            return None
        err, rng = AXUIElementCopyAttributeValue(el, kAXSelectedTextRangeAttribute, None)
        if err or rng is None:
            return None
        ok, cf = AXValueGetValue(rng, kAXValueCFRangeType, None)
        if not ok or cf.location < 1:
            return None
        prev = AXValueCreate(kAXValueCFRangeType, CFRangeMake(cf.location - 1, 1))
        err, bounds = AXUIElementCopyParameterizedAttributeValue(el, kAXBoundsForRangeParameterizedAttribute, prev, None)
        if err or bounds is None:
            return None
        ok, rect = AXValueGetValue(bounds, kAXValueCGRectType, None)
        if not ok:
            return None
        return (rect.origin.x + rect.size.width, rect.origin.y, rect.size.height)


_mon_cache = (0.0, [])


def get_monitors(root):
    """[(x, y, w, h)] for each monitor (cached a few seconds)."""
    global _mon_cache
    if time.time() - _mon_cache[0] < 3 and _mon_cache[1]:
        return _mon_cache[1]
    ms = []
    try:
        from screeninfo import get_monitors as gm
        ms = [(m.x, m.y, m.width, m.height) for m in gm()]
    except Exception:
        pass
    if not ms and WIN:
        try:
            gsm = user32.GetSystemMetrics
            ms = [(gsm(76), gsm(77), gsm(78), gsm(79))]      # virtual desktop
        except Exception:
            pass
    if not ms:
        ms = [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]
    _mon_cache = (time.time(), ms)
    return ms


def monitor_at(ms, px, py):
    for m in ms:
        if m[0] <= px < m[0] + m[2] and m[1] <= py < m[1] + m[3]:
            return m
    return ms[0]


# --------------------------------------------------------------------------
# Reading the focused text field (ground truth)
# --------------------------------------------------------------------------
@dataclass
class FieldSnap:
    text: str                           # normalized: "\n" line breaks
    caret: Optional[int] = None         # offset of the caret in `text`; None = unknown
    selection: int = 0                  # > 0: something is selected (caret not reliable)
    bounds: Optional[object] = None     # callable(a, b) -> (x, y, w, h) screen rect, or None


def _norm_nl(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


class FieldBackend:
    """read() -> FieldSnap | None (None = unsupported, empty, or a password field)."""

    def thread_context(self):
        import contextlib
        return contextlib.nullcontext()

    def read(self) -> Optional[FieldSnap]:
        return None


class WinFieldBackend(FieldBackend):
    """Win32 edit/rich-edit controls (Notepad, WordPad, many dialogs), then UI Automation
    (optional `pip install uiautomation`) for browsers, Word, modern apps."""
    CLASSIC = {"Edit", "RichEdit", "RichEdit20W", "RichEdit20A", "RICHEDIT50W", "RichEditD2DPT"}

    def __init__(self):
        self.uia_errors = 0
        self._auto = None
        try:
            import uiautomation as auto
            self._auto = auto
        except Exception:
            dbg("uiautomation not installed: only classic Win32 edit controls can be read")
        user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                               wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                                               ctypes.POINTER(ctypes.c_size_t)]
        user32.SendMessageTimeoutW.restype = wintypes.LPARAM
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    def thread_context(self):
        if self._auto is not None and hasattr(self._auto, "UIAutomationInitializerInThread"):
            return self._auto.UIAutomationInitializerInThread()
        return super().thread_context()

    def _send(self, hwnd, msg, wp=0, lp=0):
        res = ctypes.c_size_t(0)
        ok = user32.SendMessageTimeoutW(hwnd, msg, wp, lp, 0x2, 250, ctypes.byref(res))   # ABORTIFHUNG
        return res.value if ok else None

    def read(self):
        snap = self._win32()
        if snap is not None:
            return snap if snap is not False else None
        if self._auto is not None and self.uia_errors < 6:
            try:
                s = self._uia()
                self.uia_errors = 0
                return s
            except Exception as ex:
                self.uia_errors += 1
                dbg("uia read failed:", repr(ex))
        return None

    def _win32(self):
        """FieldSnap, False (refuse: password), or None (not a classic edit control)."""
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetGUIThreadInfo(0, ctypes.byref(info)) or not info.hwndFocus:
            return None
        h = info.hwndFocus
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, buf, 64)
        if buf.value not in self.CLASSIC:
            return None
        if user32.GetWindowLongW(h, -16) & 0x20 and buf.value == "Edit":     # ES_PASSWORD
            return False
        n = self._send(h, 0x000E)                                           # WM_GETTEXTLENGTH
        maxc = CFG["max_field_chars"]
        if n is None or n > maxc * 4:
            return None
        text = ctypes.create_unicode_buffer(n + 2)
        if self._send(h, 0x000D, n + 1, ctypes.addressof(text)) is None:    # WM_GETTEXT
            return None
        raw = text.value
        a, b = wintypes.DWORD(0), wintypes.DWORD(0)
        caret, sel = None, 0
        if self._send(h, 0x00B0, ctypes.addressof(a), ctypes.addressof(b)) is not None:   # EM_GETSEL
            lo, hi = min(a.value, b.value), max(a.value, b.value)
            if hi <= len(raw):
                caret = len(_norm_nl(raw[:hi]))
                sel = len(_norm_nl(raw[lo:hi]))
        return FieldSnap(_norm_nl(raw), caret, sel, None)

    def _uia(self):
        auto = self._auto
        ctrl = auto.GetFocusedControl()
        if ctrl is None or getattr(ctrl, "IsPassword", False):
            return None
        kind = ctrl.ControlTypeName
        if not (kind == "EditControl" or (kind == "DocumentControl"
                                          and foreground().name in ("winword", "wordpad"))):
            return None
        maxc = CFG["max_field_chars"]
        tp = ctrl.GetTextPattern()
        if tp:
            doc = tp.DocumentRange
            raw = doc.GetText(maxc + 1)
            caret, sel = None, 0
            sels = tp.GetSelection()
            if sels:
                r = doc.Clone()
                r.MoveEndpointByRange(auto.TextPatternRangeEndpoint.End, sels[0],
                                      auto.TextPatternRangeEndpoint.Start)
                caret = len(_norm_nl(r.GetText(maxc + 1)))
                sel = len(sels[0].GetText(5000))
            fn = None
            if "\r" not in raw:
                def fn(a, b, doc=doc):
                    try:
                        rg = doc.Clone()
                        rg.MoveEndpointByUnit(auto.TextPatternRangeEndpoint.Start, auto.TextUnit.Character, a)
                        rg.MoveEndpointByRange(auto.TextPatternRangeEndpoint.End, rg,
                                               auto.TextPatternRangeEndpoint.Start)
                        rg.MoveEndpointByUnit(auto.TextPatternRangeEndpoint.End, auto.TextUnit.Character, b - a)
                        rects = rg.GetBoundingRectangles()
                        if rects:
                            r0 = rects[0]
                            return (r0.left, r0.top, r0.right - r0.left, r0.bottom - r0.top)
                    except Exception:
                        pass
                    return None
            return FieldSnap(_norm_nl(raw), caret, sel, fn)
        vp = ctrl.GetValuePattern()
        if vp:
            return FieldSnap(_norm_nl(str(vp.Value or "")), None, 0, None)
        return None


class MacFieldBackend(FieldBackend):
    """Accessibility API (Cocoa text fields/views). Needs pyobjc-framework-ApplicationServices."""
    ROLES = {"AXTextField", "AXTextArea", "AXComboBox"}

    def read(self):
        from ApplicationServices import (
            AXUIElementCreateSystemWide, AXUIElementCopyAttributeValue,
            AXUIElementCopyParameterizedAttributeValue, AXValueGetValue, AXValueCreate,
            kAXFocusedUIElementAttribute, kAXRoleAttribute, kAXSubroleAttribute, kAXValueAttribute,
            kAXSelectedTextRangeAttribute, kAXBoundsForRangeParameterizedAttribute,
            kAXValueCFRangeType, kAXValueCGRectType)
        from CoreFoundation import CFRangeMake
        err, el = AXUIElementCopyAttributeValue(AXUIElementCreateSystemWide(), kAXFocusedUIElementAttribute, None)
        if err or el is None:
            return None
        err, role = AXUIElementCopyAttributeValue(el, kAXRoleAttribute, None)
        err2, sub = AXUIElementCopyAttributeValue(el, kAXSubroleAttribute, None)
        if err or str(role) not in self.ROLES or (not err2 and "Secure" in str(sub)):
            return None
        err, val = AXUIElementCopyAttributeValue(el, kAXValueAttribute, None)
        if err or val is None:
            return None
        text = _norm_nl(str(val))
        if len(text) > CFG["max_field_chars"] * 2:
            return None
        caret, sel = None, 0
        err, rng = AXUIElementCopyAttributeValue(el, kAXSelectedTextRangeAttribute, None)
        astral = any(ord(c) > 0xFFFF for c in text)
        if not err and rng is not None and not astral:
            ok, cf = AXValueGetValue(rng, kAXValueCFRangeType, None)
            if ok:
                caret, sel = int(cf.location), int(cf.length)
                if caret > len(text):
                    caret = None

        def fn(a, b):
            try:
                r = AXValueCreate(kAXValueCFRangeType, CFRangeMake(a, max(b - a, 1)))
                e, bounds = AXUIElementCopyParameterizedAttributeValue(el, kAXBoundsForRangeParameterizedAttribute, r, None)
                if e or bounds is None:
                    return None
                ok, rect = AXValueGetValue(bounds, kAXValueCGRectType, None)
                return (rect.origin.x, rect.origin.y, rect.size.width, rect.size.height) if ok else None
            except Exception:
                return None
        return FieldSnap(text, caret, sel, None if astral else fn)


def make_field_backend() -> Optional[FieldBackend]:
    if not CFG["read_field"]:
        return None
    try:
        if WIN:
            return WinFieldBackend()
        if MAC:
            return MacFieldBackend()
    except Exception as ex:
        dbg("no field backend:", repr(ex))
    return None


# --------------------------------------------------------------------------
# Model: follows the typed text, owns the issue list, applies fixes
# --------------------------------------------------------------------------
MAX_STATIC = 20000


class Assistant:
    """Tracks text typed in the current field (live mode) or a clipboard snapshot (static mode).

    Invariant (live mode): `text` is a contiguous run of characters typed in the focused field
    and `caret` is the real caret position inside it. Anything that could break that invariant
    (mouse click, arrow past the edges, Ctrl shortcuts, focus change, foreign key injection)
    calls reset() - we never guess.
    """

    def __init__(self, notify, toast, kb, keys, locator=None, lex: Lexicon = None, backend=None):
        self.notify, self.toast, self.kb, self.K, self.locator = notify, toast, kb, keys, locator
        self.backend = backend
        self.lex = lex or LEX
        self.lock = threading.RLock()
        self.inject_lock = threading.Lock()
        self.stop = threading.Event()
        self.mode = "live"                   # live | clipboard
        self.text, self.caret = "", 0
        self.start_sentence = False          # is buffer start known to be a sentence start?
        self.issues: list = []
        self.selected: Optional[str] = None
        self.ignored: set = set(load_ignored())
        self.rev = 0
        self.checked_part = self.checked_full = -1
        self.last_edit = time.time()
        self.paused = False
        self.injecting = False
        self.inject_until = 0.0
        self.mods: set = set()
        self.app_ignored: Optional[str] = None
        self.target = Target()
        self.samples: dict = {}
        self.sample_q: queue.Queue = queue.Queue()
        self._dict_sig = None
        self._dict_checked = 0.0
        self.disk_check = 0.0
        # field sync state (backend present): the real field text is ground truth
        self.stale = False                   # model may differ from the field: re-read before trusting
        self.complete = False                # `text` covers the whole field (edges are real edges)
        self.caret_known = True
        self.field_ok: Optional[bool] = None # can the focused field be read? (None = not tried yet)
        self.base = 0                        # offset of `text` inside a very long field
        self.want_sync = 0.0
        self.verified_rev = -1
        self.last_verify = 0.0
        self.expect_toast = False
        self.read_backoff_until = 0.0
        self.sync_event = threading.Event()
        self.sync_done = threading.Event()
        self.bounds_fn = None
        self.bounds_due = 0.0
        self.sync_gen = 0
        self._reading = False
        ensure_dictionary_folder()
        self.reload_dictionaries(quiet=True)
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)

    def start(self):
        self._thread.start()
        if self.backend:
            self._sync_thread.start()
            self.want_sync = time.time() + 0.2

    # ---------------- state
    def snapshot(self) -> dict:
        with self.lock:
            return {
                "mode": self.mode, "text": self.text, "caret": self.caret, "paused": self.paused,
                "app_ignored": self.app_ignored, "selected": self.selected, "rev": self.rev,
                "busy": self.injecting, "stale": self.stale, "synced": bool(self.field_ok),
                "issues": [dict(key=i.key, kind=i.kind, rule=i.rule, message=i.message, hs=i.hs,
                                he=i.he, word=i.word, sugg=list(i.sugg), auto=i.auto,
                                rect=i.rect, origin=i.origin)
                           for i in sorted(self.issues, key=lambda x: (x.hs, x.he))],
            }

    def _bump(self):
        self.rev += 1
        self.last_edit = time.time()

    def reset(self, new_line: bool = False, new_field: bool = False):
        with self.lock:
            if self.mode != "live":
                return
            had = bool(self.text or self.issues)
            self.text, self.caret, self.issues, self.selected = "", 0, [], None
            self.start_sentence = new_line
            self.stale, self.complete, self.caret_known, self.base = False, False, True, 0
            if new_field:
                self.field_ok = None
            self.samples.clear()
            self.bounds_fn = None
            if self.backend:
                self.want_sync = time.time() + 0.1
            self._bump()
        if had:
            self.notify()

    def invalidate(self):
        """Something happened that may have changed the field in ways we can't follow."""
        with self.lock:
            if self.mode != "live":
                return
            if self.backend and self.field_ok is not False and not self.paused and not self.app_ignored:
                self.stale = True
                self.want_sync = time.time() + 0.1
                self.last_edit = time.time()
            else:
                self.reset()
                return
        self.notify()

    def _gate(self) -> bool:
        """May a key event edit the model right now? (call with the lock held)"""
        if self.mode != "live" or self.paused or self.app_ignored or self.injecting:
            return False
        if self.stale or not self.caret_known:
            self.last_edit = time.time()
            if not self.stale:
                self.invalidate()
            return False
        return True

    def _live_ok(self) -> bool:
        return self.mode == "live" and not self.paused and not self.app_ignored and not self.injecting

    # ---------------- selection / dismissal
    def _find(self, key):
        return next((i for i in self.issues if i.key == key), None)

    def select(self, key):
        with self.lock:
            if self._find(key):
                self.selected = key
        self.notify()

    def step(self, d: int = 1):
        with self.lock:
            order = sorted(self.issues, key=lambda x: (x.hs, x.he))
            if not order:
                return
            keys = [i.key for i in order]
            i = (keys.index(self.selected) + d) % len(keys) if self.selected in keys else 0
            self.selected = keys[i]
        self.notify()

    def _ensure_selection(self):
        if self.selected in {i.key for i in self.issues}:
            return
        order = sorted(self.issues, key=lambda x: x.hs)
        if not order:
            self.selected = None
            return
        before = [i for i in order if i.hs <= self.caret]
        self.selected = (before[-1] if before and self.mode == "live" else order[0]).key

    def ignore(self, key=None):
        with self.lock:
            it = self._find(key or self.selected)
            if not it:
                return
            tag = f"{it.rule}|{it.word.lower()}"
            self.ignored.add(tag)
            save_ignored(tag)
            self.issues = [i for i in self.issues if f"{i.rule}|{i.word.lower()}" != tag]
            self._ensure_selection()
        self.notify()

    def add_word(self, key=None):
        with self.lock:
            it = self._find(key or self.selected)
            if not it or it.kind != "spelling":
                return
            lw = it.word.lower().replace("\u2019", "'")
            try:
                ensure_dictionary_folder()
                with open(MY_WORDS, "a", encoding="utf-8") as f:
                    f.write(lw + "\n")
                self._dict_sig = dictionary_signature()
            except Exception as ex:
                dbg("could not save word:", ex)
            self.lex.set_user_words(self.lex.user | {lw})
            self.issues = [i for i in self.issues if not (i.kind == "spelling" and i.word.lower() == lw)]
            self._ensure_selection()
        self.notify()
        self.toast(f"Added “{lw}” to your dictionary")

    def toggle_pause(self):
        with self.lock:
            self.paused = not self.paused
            p = self.paused
        self.reset()
        self.toast("Paused — press the hotkey again to resume" if p else "Checking resumed")
        self.notify()

    def reload_dictionaries(self, quiet: bool = False):
        sig = dictionary_signature()
        if sig == self._dict_sig:
            return
        first = self._dict_sig is None
        self._dict_sig = sig
        words = load_all_dictionaries()
        with self.lock:
            self.lex.set_user_words(words)
            self.issues = [i for i in self.issues if not (i.kind == "spelling" and i.word.lower() in words)]
            self._bump()
            self.checked_part = self.checked_full = -1
        dbg("dictionary loaded:", len(words), "words")
        if not quiet and not first:
            self.toast(f"Dictionary reloaded ({len(words):,} words)")
        self.notify()

    # ---------------- key events (live mode)
    def _shift(self, a: int, b: int, ins_len: int, ins_is_word: bool = False):
        """Text [a,b) was replaced by ins_len characters: move or drop issue spans accordingly."""
        delta = ins_len - (b - a)
        keep = []
        for i in self.issues:
            lo, hi = min(i.s, i.hs), max(i.e, i.he)
            if hi < a or (hi == a and not ins_is_word):
                keep.append(i)                        # entirely before the edit: still valid
            elif lo >= b:
                i.s += delta; i.e += delta; i.hs += delta; i.he += delta
                i.rect = None
                keep.append(i)                        # entirely after: slide along
            # overlapping the edit: verdict is stale, drop it
        self.issues = keep
        self.samples = {k: v for k, v in self.samples.items() if k <= a}

    def type_char(self, c: str):
        with self.lock:
            if not self._gate():
                return
            p = self.caret
            self.text = self.text[:p] + c + self.text[p:]
            self.caret = p + 1
            self._shift(p, p, 1, ins_is_word=c.isalnum() or c in "'\u2019")
            self._trim()
            self._bump()
            self.sample_q.put(self.caret)
            self._ensure_selection()
        self.notify()

    def backspace(self):
        with self.lock:
            if not self._gate():
                return
            p = self.caret
            if p == 0:
                if not self.complete:
                    self.start_sentence = False   # deleting text before our window
                return
            self.text = self.text[:p - 1] + self.text[p:]
            self.caret = p - 1
            self._shift(p - 1, p, 0)
            self._bump()
            self.sample_q.put(self.caret)
            self._ensure_selection()
        self.notify()

    def delete_fwd(self):
        with self.lock:
            if not self._gate() or self.caret >= len(self.text):
                return
            p = self.caret
            self.text = self.text[:p] + self.text[p + 1:]
            self._shift(p, p + 1, 0)
            self._bump()
            self._ensure_selection()
        self.notify()

    def move(self, d: int):
        """Left/Right arrow. Leaving a partial buffer means the caret is unknown: re-sync."""
        with self.lock:
            if not self._gate():
                return
            n = self.caret + d
            if 0 <= n <= len(self.text):
                self.caret = n
                self.last_edit = time.time()
                self.sample_q.put(n)
                lost = False
            else:
                lost = not self.complete          # at a real field edge the key does nothing
        if lost:
            self.invalidate()
        else:
            self.notify()

    def enter(self):
        if self.backend and self.field_ok:
            self.invalidate()                     # chat boxes clear, editors add a line: read the truth
        elif CFG["enter_resets"]:
            self.reset(new_line=True)
        else:
            self.type_char("\n")

    def drop_marks(self):
        with self.lock:
            self.samples.clear()
            self.bounds_due = time.time() + 0.3
            if not self.bounds_fn:
                for i in self.issues:
                    i.rect = None
        self.notify()

    def _trim(self):
        cap = CFG["max_field_chars"] if self.complete else CFG["max_buffer"]
        if len(self.text) <= cap:
            return
        cut = len(self.text) - (cap - 200)
        self.text = self.text[cut:]
        self.caret -= cut
        self.start_sentence = False
        self.complete = False
        keep = []
        for i in self.issues:
            if min(i.s, i.hs) >= cut:
                i.s -= cut; i.e -= cut; i.hs -= cut; i.he -= cut
                keep.append(i)
        self.issues = keep
        self.samples = {k - cut: v for k, v in self.samples.items() if k >= cut}
        if self.caret < 0:
            self.text, self.caret, self.issues = "", 0, []

    # ---------------- clipboard (static) mode
    def load_static(self, text: str):
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            self.toast("The clipboard has no text to check")
            return
        trunc = len(text) > MAX_STATIC
        with self.lock:
            self.mode = "clipboard"
            self.text, self.caret = text[:MAX_STATIC], 0
            self.start_sentence = True
            self.issues, self.selected = [], None
            self.samples.clear()
            self._bump()
            self.checked_part = self.checked_full = -1
        if trunc:
            self.toast(f"Checking the first {MAX_STATIC:,} characters")
        self.notify()

    def exit_static(self):
        with self.lock:
            if self.mode != "clipboard":
                return
            self.mode = "live"
            self.text, self.issues, self.selected, self.caret = "", [], None, 0
            self.start_sentence = False
            self._bump()
        self.notify()

    def static_text(self) -> str:
        with self.lock:
            return self.text

    # ---------------- analysis worker
    def _active_span(self):
        t, c = self.text, self.caret
        wc = lambda ch: ch.isalnum() or ch in "'\u2019"
        a = c
        while a > 0 and wc(t[a - 1]):
            a -= 1
        b = c
        while b < len(t) and wc(t[b]):
            b += 1
        return (a, b) if b > a else None

    def _maybe_analyze(self):
        with self.lock:
            if self.injecting or not self.text or (self.stale and self.mode == "live"):
                return
            if self.mode == "live" and (self.paused or self.app_ignored):
                return
            rev = self.rev
            if rev == self.checked_full:
                return
            idle = time.time() - self.last_edit
            live = self.mode == "live"
            full = (not live) or idle >= CFG["check_trailing_word_seconds"]
            if not full and not (idle >= CFG["idle_check_seconds"] and rev != self.checked_part):
                return
            active = None if full else self._active_span()
            text, start, ignored = self.text, self.start_sentence, frozenset(self.ignored)
        try:
            found = check_text(text, lex=self.lex, cfg=CFG, start_is_sentence=start, active=active,
                               ignored=ignored)
        except Exception as ex:                       # a rule bug must never kill the worker
            dbg("check failed:", repr(ex))
            with self.lock:
                self.checked_part = self.checked_full = rev
            return
        with self.lock:
            if self.rev != rev or self.injecting:
                return                                # text changed while checking: try again
            self.issues = found
            self.checked_part = rev
            if full or active is None:
                self.checked_full = rev
            self._ensure_selection()
            self._refresh_rects()
            self.bounds_due = time.time()
        self.notify()

    def _worker(self):
        last_forget_check = 0.0
        while not self.stop.is_set():
            try:
                pos = self.sample_q.get(timeout=0.08)
            except queue.Empty:
                pos = None
            try:
                if pos is not None:
                    if self.sample_q.qsize() > 8:
                        while not self.sample_q.empty():
                            self.sample_q.get_nowait()
                    self._take_sample(pos)
                self._schedule_sync()
                self._maybe_analyze()
                now = time.time()
                if now - last_forget_check > 2:
                    last_forget_check = now
                    if now - self.disk_check > 3:
                        self.disk_check = now
                        self.reload_dictionaries()
                    with self.lock:
                        stale = (self.mode == "live" and self.text and not self.injecting
                                 and now - self.last_edit > CFG["forget_after_seconds"])
                    if stale:
                        self.reset()
            except Exception as ex:
                dbg("worker error:", repr(ex))

    # ---------------- reading the real field (ground truth)
    def _schedule_sync(self):
        if not self.backend:
            return
        now = time.time()
        with self.lock:
            if self.mode != "live" or self.paused or self.app_ignored or self.injecting:
                return
            if now < self.read_backoff_until:
                return
            idle = now - self.last_edit
            go = False
            if self.want_sync and now >= self.want_sync and idle >= 0.12:
                self.want_sync, go = 0.0, True
            elif self.field_ok and idle >= 0.45 and (self.verified_rev != self.rev or now - self.last_verify > 5.0):
                go = True
        if go:
            self.sync_event.set()

    def _sync_loop(self):
        with self.backend.thread_context():
            while not self.stop.is_set():
                if self.sync_event.wait(0.4):
                    self.sync_event.clear()
                    self._read_cycle()
                elif self.bounds_fn and self.bounds_due and time.time() >= self.bounds_due:
                    self._bounds_pass()

    def _read_cycle(self):
        snap = None
        try:
            with self.lock:
                if self.mode != "live" or self.paused or self.app_ignored or self.injecting:
                    return
                rev0 = self.rev
                self._reading = True
            t0 = time.time()
            try:
                snap = self.backend.read()
            except Exception as ex:
                dbg("field read failed:", repr(ex))
            if time.time() - t0 > 1.5:
                self.read_backoff_until = time.time() + 10      # slow/hanging app: leave it alone
            self._adopt(snap, rev0)
            with self.lock:
                self.bounds_fn = snap.bounds if snap else None
                self.bounds_due = time.time() if self.bounds_fn else 0.0
            if self.bounds_fn:
                self._bounds_pass()
        except Exception as ex:
            dbg("read cycle error:", repr(ex))
        finally:
            with self.lock:
                self._reading = False
                self.sync_gen += 1

    def _sync_now(self, timeout: float = 1.5):
        """Force a read and wait for it (used right before injecting keystrokes)."""
        with self.lock:
            target = self.sync_gen + (2 if self._reading else 1)
        self.sync_event.set()
        end = time.time() + timeout
        while time.time() < end and self.sync_gen < target:
            time.sleep(0.02)

    def _adopt(self, snap, rev0: int):
        now = time.time()
        mismatch = False
        with self.lock:
            if self.mode != "live" or self.paused or self.app_ignored or self.injecting:
                return
            self.last_verify = now
            if snap is None:
                self.field_ok = False
                if self.stale:                    # this field cannot be followed: typed-only tracking
                    self.stale = False
                    self.text, self.caret, self.issues, self.selected = "", 0, [], None
                    self.complete, self.caret_known, self.start_sentence, self.base = False, True, False, 0
                    self._bump()
                self.verified_rev = self.rev
            else:
                self.field_ok = True
                if not self.stale and self.rev != rev0:
                    return                        # the user typed during the read: verify again later
                text, caret, sel = snap.text, snap.caret, snap.selection
                maxc, base, complete = CFG["max_field_chars"], 0, True
                if len(text) > maxc:
                    c = caret if caret is not None else 0
                    base = max(0, min(c - maxc // 2, len(text) - maxc))
                    text, complete = text[base:base + maxc], False
                    if caret is not None:
                        caret -= base
                        if not 0 <= caret <= len(text):
                            caret = None
                known = caret is not None and sel == 0
                if text == self.text and base == self.base:      # same text: keep issues, fix the caret
                    self.caret_known = known
                    if known:
                        self.caret = caret
                    self.stale, self.complete = False, complete
                    self.verified_rev, self.expect_toast = self.rev, False
                else:                                           # the field is the truth: adopt it
                    mismatch = self.expect_toast and not self.stale
                    self.text, self.base = text, base
                    self.caret = caret if caret is not None else len(text)
                    self.caret_known, self.complete = known, complete
                    self.start_sentence = base == 0
                    self.issues, self.selected = [], None
                    self.samples.clear()
                    self.stale = False
                    self._bump()
                    self.checked_part = self.checked_full = -1
                    self.verified_rev, self.expect_toast = self.rev, False
        if mismatch:
            self.toast("The text didn't change as expected — re-synced")
        self.notify()

    def adopt_probe(self, text: str):
        """Whole-field text obtained by the explicit clipboard scan (caret is at the end)."""
        text = _norm_nl(text)[:CFG["max_field_chars"]]
        with self.lock:
            if self.mode != "live":
                return
            self.text, self.caret, self.base = text, len(text), 0
            self.complete, self.caret_known, self.stale, self.start_sentence = True, True, False, True
            self.issues, self.selected = [], None
            self.samples.clear()
            self._bump()
            self.checked_part = self.checked_full = -1
        self.notify()
        self.toast(f"Scanned {len(text):,} characters")

    def _bounds_pass(self):
        fn = self.bounds_fn
        if not fn:
            return
        with self.lock:
            rev, base = self.rev, self.base
            items = [(i.key, i.hs + base, i.he + base) for i in self.issues[:40] if i.he > i.hs]
            self.bounds_due = 0.0
        ox, oy = window_origin()
        res = {}
        for key, a, b in items:
            r = fn(a, b)
            if r and r[2] > 2:
                res[key] = (int(r[0]), int(r[1] + r[3] - 1), int(r[2]))
        with self.lock:
            if self.rev != rev:
                return
            for i in self.issues:
                i.rect = res.get(i.key)
                i.origin = (ox, oy)
        self.notify()

    # ---------------- caret samples -> underline rectangles
    def _take_sample(self, pos: int):
        if not (self.locator and CFG["underlines"]):
            return
        time.sleep(0.04)                              # let the app move its caret
        loc = self.locator.get()
        if not loc:
            return
        ox, oy = window_origin()
        with self.lock:
            if self.mode != "live" or pos != self.caret or pos > len(self.text) or self.injecting:
                return
            prev = self.samples.get(pos - 1)
            if prev and abs(prev[1] - loc[1]) > loc[2] * 0.6:
                ws = pos
                while ws > 0 and not self.text[ws - 1].isspace():
                    ws -= 1                           # the word that wrapped moved: forget its samples
                self.samples = {k: v for k, v in self.samples.items() if k < ws}
            self.samples[pos] = (loc[0], loc[1], loc[2], ox, oy)
            self._refresh_rects()
        self.notify()

    def _sample_at(self, pos: int):
        s = self.samples.get(pos)
        if s:
            return s
        lo = hi = None
        for k in self.samples:
            if k < pos and (lo is None or k > lo):
                lo = k
            elif k > pos and (hi is None or k < hi):
                hi = k
        if lo is None or hi is None or hi - lo > 60:
            return None
        a, b = self.samples[lo], self.samples[hi]
        if (a[3], a[4]) != (b[3], b[4]) or abs(a[1] - b[1]) > max(6, a[2] * 0.6):
            return None
        t = (pos - lo) / (hi - lo)
        return (a[0] + (b[0] - a[0]) * t, a[1], a[2], a[3], a[4])

    def _refresh_rects(self):
        if self.bounds_fn:
            return                                # exact rectangles come from the accessibility API
        for it in self.issues:
            it.rect = None
            if self.mode != "live" or it.he <= it.hs or not self.samples:
                continue
            a, b = self._sample_at(it.hs), self._sample_at(it.he)
            if not a or not b or (a[3], a[4]) != (b[3], b[4]) or abs(a[1] - b[1]) > max(6, a[2] * 0.6):
                continue
            w = b[0] - a[0]
            if 3 <= w <= 1500:
                it.rect = (int(a[0]), int(a[1] + a[2] - 1), int(w))
                it.origin = (a[3], a[4])

    # ---------------- applying fixes
    def apply(self, key, idx: int):
        self._start_apply([(key, idx)])

    def apply_selected(self, idx: int):
        with self.lock:
            key = self.selected
        if key:
            self.apply(key, idx)

    NEAR = 1500                                       # in long texts, fix-all works near the caret

    def fix_all(self):
        with self.lock:
            autos = [i for i in self.issues if i.auto and i.sugg]
            far = 0
            if self.mode == "live" and len(self.text) > 4000:
                near = [i for i in autos if abs(i.s - self.caret) <= self.NEAR]
                far, autos = len(autos) - len(near), near
            plan = [(i.key, 0) for i in autos]
        if far:
            self.toast(f"Fixing the issues near your cursor; {far} farther away are left for you")
        if not plan:
            self.toast("Nothing to fix automatically")
            return
        self._start_apply(plan)

    def _start_apply(self, plan):
        if self.mode == "clipboard":
            self._apply_static(plan)
        else:
            threading.Thread(target=self._do_apply, args=(plan,), daemon=True).start()

    def _choose(self, plan):
        """[(issue, replacement)] sorted right-to-left with overlaps removed. Call with lock held."""
        items = []
        for key, idx in plan:
            it = self._find(key)
            if it and 0 <= idx < len(it.sugg):
                items.append((it, it.sugg[idx]))
        items.sort(key=lambda t: (-t[0].s, -t[0].e))
        chosen, limit = [], None
        for it, rp in items:
            if limit is not None and it.e > limit:
                continue
            chosen.append((it, rp))
            limit = it.s
        return chosen

    def _apply_static(self, plan):
        with self.lock:
            chosen = self._choose(plan)
            for it, rp in chosen:
                self.text = self.text[:it.s] + rp + self.text[it.e:]
            self.issues = []
            self._bump()
            self.checked_part = self.checked_full = -1
        self.notify()
        if chosen:
            self.toast(f"Fixed {len(chosen)} issue{'s' if len(chosen) != 1 else ''}")

    def _tap(self, key, n: int):
        for i in range(n):
            self.kb.tap(key)
            if i % 12 == 11:
                time.sleep(0.004)                     # keystrokes queue up in the app; just don't flood

    def _goto(self, pos: int):
        d = pos - self.caret
        if d:
            self._tap(self.K.right if d > 0 else self.K.left, abs(d))
            with self.lock:
                self.caret = pos

    def _do_apply(self, plan):
        deadline = time.time() + 1.5                  # wait until the hotkey modifiers are released
        while self.mods and time.time() < deadline:
            time.sleep(0.02)
        if self.mods:
            self.toast("Release the modifier keys, then try again")
            return
        if not self.inject_lock.acquire(blocking=False):
            return
        done = 0
        try:
            if self.backend and self.field_ok:
                self._sync_now()                      # pre-flight: is our picture of the field still right?
            with self.lock:
                if not self._live_ok():
                    return
                if self.stale or not self.caret_known:
                    self.toast("Can't place the cursor yet (text selected or still syncing) — try again")
                    return
                chosen = self._choose(plan)
                if not chosen:
                    self.toast("The text changed meanwhile — suggestions refreshed")
                    return
                c0 = self.caret
                lo = min([c0] + [it.s for it, _ in chosen])
                hi = max([c0] + [it.e for it, _ in chosen])
                if hi - lo > 6000:
                    self.toast("That fix is far from your cursor — click near it and try again")
                    return
                if not text_is_safe_to_navigate(self.text[lo:hi]):
                    self.toast("Can't fix safely here (emoji or special characters in the way)")
                    return
                self.injecting = True
            self._ensure_focus()
            for it, rp in chosen:
                with self.lock:
                    old = self.text[it.s:it.e]
                    off, dl, ins = minimal_edit(old, rp)
                    p = it.s + off
                    end = p + dl
                self._goto(end)
                self._tap(self.K.backspace, dl)
                if ins:
                    self.kb.type(ins)
                with self.lock:
                    self.text = self.text[:p] + ins + self.text[end:]
                    self.caret = p + len(ins)
                    self.issues = [i for i in self.issues if i.key != it.key]
                    self._shift(p, end, len(ins))
                    if c0 >= end:
                        c0 += len(ins) - dl
                    elif c0 > p:
                        c0 = p + len(ins)
                done += 1
            self._goto(max(0, min(c0, len(self.text))))
            time.sleep(0.15)
        except Exception as ex:
            dbg("apply failed:", repr(ex))
            self.injecting = False
            self.invalidate() if self.backend else self.reset()
            self.toast("Stopped for safety — re-checking the field")
            return
        finally:
            self.inject_until = time.time() + 0.12
            with self.lock:
                self.injecting = False
                self._bump()
                self.checked_part = self.checked_full = -1
                self.verified_rev = -1                # verify the result against the real field
                self.expect_toast = bool(done and self.backend and self.field_ok)
            self.inject_lock.release()
        self.notify()
        if done:
            self.toast(f"Fixed {done} issue{'s' if done != 1 else ''}")

    def _ensure_focus(self):
        """If our own card has focus (e.g. macOS after a click), hand it back before typing."""
        cur = foreground()
        if cur.pid is not None and cur.pid == os.getpid() and self.target.wid is not None:
            if activate(self.target):
                time.sleep(0.15)
            else:
                raise RuntimeError("could not return focus to the target window")


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------
MAC_VK = {18: "1", 19: "2", 20: "3", 45: "n", 11: "b", 2: "d", 13: "w", 3: "f", 35: "p",
          12: "q", 8: "c", 9: "v", 31: "o", 0: "a"}
IGNORED_KEY_NAMES = {"caps_lock", "num_lock", "scroll_lock", "print_screen", "pause",
                     "media_volume_up", "media_volume_down", "media_volume_mute",
                     "media_play_pause", "media_next", "media_previous"}


def key_name(key) -> Optional[str]:
    """Lower-case letter/digit of a key, robust to Ctrl/Alt changing .char."""
    vk, ch = getattr(key, "vk", None), getattr(key, "char", None)
    if MAC and vk in MAC_VK:
        return MAC_VK[vk]
    if vk is not None:
        if WIN and (48 <= vk <= 57 or 65 <= vk <= 90):
            return chr(vk).lower()
        if not WIN and not MAC and (48 <= vk <= 57 or 65 <= vk <= 90 or 97 <= vk <= 122):
            return chr(vk).lower()
    if ch and len(ch) == 1:
        o = ord(ch)
        return chr(o + 96) if 1 <= o <= 26 else ch.lower()
    return None


class Keys:
    """Global keyboard/mouse listener callbacks. Never raises into pynput's thread."""

    def __init__(self, model: Assistant, ui_q: queue.Queue):
        self.m, self.q = model, ui_q
        self.ui = None
        Key = keyboard.Key

        def ks(*names):
            return {getattr(Key, n) for n in names if hasattr(Key, n)}
        self.groups = {
            "ctrl": ks("ctrl", "ctrl_l", "ctrl_r"),
            "alt": ks("alt", "alt_l", "alt_r"),
            "shift": ks("shift", "shift_l", "shift_r"),
            "cmd": ks("cmd", "cmd_l", "cmd_r"),
        }
        self.altgr = ks("alt_gr")
        self.mod_keys = set().union(*self.groups.values()) | self.altgr
        self.Key = Key

    def held(self, group: str) -> bool:
        return bool(self.m.mods & self.groups[group])

    def chord_active(self) -> bool:
        if self.m.mods & self.altgr:
            return False                       # AltGr (= Ctrl+Alt on Windows) types characters
        return all(self.held(g) for g in CFG["hotkey_modifiers"])

    def on_press(self, key):
        try:
            self._press(key)
        except Exception as ex:
            dbg("key handler error:", repr(ex))
            try:
                self.m.reset()
            except Exception:
                pass

    def _press(self, key):
        m, Key = self.m, self.Key
        if key in self.mod_keys:
            m.mods.add(key)
            return
        if m.injecting or time.time() < m.inject_until:
            return                             # echo of our own injected keystrokes
        if self.chord_active():
            if self._hotkey(key_name(key)):
                return
            m.invalidate()                     # some other Ctrl+Alt combo: unknown effect
            return
        if m.mode != "live" or m.paused or m.app_ignored:
            return
        altgr = bool(m.mods & self.altgr)
        if not altgr:
            if self.held("ctrl") or self.held("cmd"):
                m.invalidate()                 # Ctrl/Cmd shortcuts (paste, undo...) change text: re-read
                return
            if self.held("alt") and not MAC:
                m.invalidate()
                return
        name = getattr(key, "name", "")
        if name in IGNORED_KEY_NAMES:
            return
        if MAC and self.held("alt") and not (getattr(key, "char", None) and key.char.isprintable()):
            m.invalidate()                     # Option+Arrow / Option+Backspace jump by words
            return
        shift = self.held("shift")
        if key == Key.space:
            m.type_char(" ")
        elif key == Key.backspace:
            m.backspace()
        elif key == Key.delete:
            m.delete_fwd()
        elif key == Key.enter:
            m.enter()
        elif key in (Key.left, Key.right) and not shift:
            m.move(-1 if key == Key.left else 1)
        elif getattr(key, "char", None):
            if key.char.isprintable():
                m.type_char(key.char)
            else:
                m.invalidate()
        else:
            m.invalidate()                     # Shift+arrows, Up/Down, Tab, Home/End, F-keys...

    def _hotkey(self, n: Optional[str]) -> bool:
        m = self.m
        if n in ("1", "2", "3"):
            m.apply_selected(int(n) - 1)
        elif n == "n":
            m.step(1)
        elif n == "b":
            m.step(-1)
        elif n == "d":
            m.ignore()
        elif n == "w":
            m.add_word()
        elif n == "f":
            m.fix_all()
        elif n == "p":
            m.toggle_pause()
        elif n == "a":
            self.q.put(("scan",))
        elif n == "c":
            self.q.put(("collapse",))
        elif n == "v":
            self.q.put(("clipboard",))
        elif n == "o":
            ensure_dictionary_folder()
            open_folder(DICT_DIR)
        elif n == "q":
            self.q.put(("quit",))
        else:
            return False
        return True

    def on_release(self, key):
        self.m.mods.discard(key)

    def on_click(self, x, y, button, pressed):
        try:
            if not pressed or self.m.injecting:
                return
            if self.ui and self.ui.point_in_card(x, y):
                return                         # clicking OUR card must not clear the issues
            self.m.invalidate()
        except Exception as ex:
            dbg("click handler error:", repr(ex))

    def on_scroll(self, *a):
        try:
            self.m.drop_marks()
        except Exception:
            pass


def watch_focus(model: Assistant, stop: threading.Event):
    """Reset on window change, remember the target window, flag ignored apps."""
    last = None
    while not stop.wait(0.25):
        t = foreground()
        if t.wid is None or t.pid == os.getpid():
            continue
        if last is not None and t.wid != last:
            model.mods.clear()                 # resync in case a key-release was missed
            model.reset(new_field=True)
        last = t.wid
        model.target = t
        ig = t.name if app_is_ignored(t.name, CFG["ignore_apps"]) else None
        if ig != model.app_ignored:
            model.app_ignored = ig
            model.reset(new_field=True)
            model.notify()


# --------------------------------------------------------------------------
# UI (Tk, main thread only)
# --------------------------------------------------------------------------
THEMES = {
    "dark": dict(bg="#111827", panel="#1f2937", border="#374151", fg="#f3f4f6", muted="#9ca3af",
                 chip="#273449", chip_hover="#334155", primary="#2563eb", primary_hover="#1d4ed8",
                 sel="#374151", good="#34d399"),
    "light": dict(bg="#ffffff", panel="#f3f4f6", border="#d1d5db", fg="#111827", muted="#6b7280",
                  chip="#e5e7eb", chip_hover="#d1d5db", primary="#2563eb", primary_hover="#1d4ed8",
                  sel="#dbeafe", good="#059669"),
}
KIND_COLOR = {"spelling": "#ef4444", "grammar": "#f59e0b", "punct": "#3b82f6", "style": "#8b5cf6"}
KIND_LABEL = {"spelling": "SPELLING", "grammar": "GRAMMAR", "punct": "PUNCTUATION", "style": "STYLE"}
PREVIEW_CHARS = 150
MAX_UNDERLINES = 24
CARD_MARGIN, BOTTOM_MARGIN = 16, 72


def pick_font_family(root) -> str:
    try:
        fams = set(tkfont.families(root))
    except Exception:
        fams = set()
    for f in ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter", "Noto Sans", "DejaVu Sans",
              "Helvetica", "Arial"):
        if f in fams:
            return f
    return "TkDefaultFont"


class UI:
    def __init__(self, root, model: Assistant, q=None):
        self.root, self.model, self.q = root, model, q
        self.th = THEMES[CFG["theme"]]
        fam = pick_font_family(root)
        self.f_ui = (fam, 10)
        self.f_bold = (fam, 10, "bold")
        self.f_small = (fam, 9)
        self.f_tiny = (fam, 8)
        self.f_big = (fam, 15, "bold")
        self.f_title = (fam, 11, "bold")
        self.collapsed = False
        self.user_pos = None
        self.card_rect = None
        self.snap = None
        self.toast_text, self.toast_until = "", 0.0
        self._sig = None
        self._rendered_at = 0.0
        self._pending = None
        self._drag = (0, 0)
        self._anchor = None
        self._line_state: dict = {}
        mods = "+".join(m.capitalize() for m in CFG["hotkey_modifiers"])
        self.hint = (f"{mods} + 1-3 apply · N/B next/prev · D ignore · W add word · F fix all · "
                     f"A scan field · V clipboard · C collapse · P pause · Q quit")

        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-alpha", 0.98)
        except tk.TclError:
            pass
        self.outer = tk.Frame(self.win, bg=self.th["border"], padx=1, pady=1)
        self.outer.pack(fill="both", expand=True)
        self.frame = tk.Frame(self.outer, bg=self.th["bg"], padx=12, pady=10)
        self.frame.pack(fill="both", expand=True)
        # map once off-screen so the window can be flagged non-activating BEFORE it is ever shown
        self.win.geometry("+-4000+-4000")
        self.win.deiconify()
        self.win.update()
        win_style(self.win)
        self.win.withdraw()
        self.lines = [self._make_line() for _ in range(MAX_UNDERLINES)] if CFG["underlines"] and (WIN or MAC) else []

    def _make_line(self):
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.geometry("1x1+-4000+-4000")
        w.update()
        win_style(w, click_through=True)
        w.withdraw()
        return w

    # ---------------- small widgets
    def label(self, parent, text, font=None, fg=None, bg=None, **kw):
        return tk.Label(parent, text=text, font=font or self.f_ui, fg=fg or self.th["fg"],
                        bg=bg or self.th["bg"], anchor="w", justify="left", **kw)

    def button(self, parent, text, cmd, kind="chip"):
        th = self.th
        bg, hover, fg = {
            "chip": (th["chip"], th["chip_hover"], th["fg"]),
            "primary": (th["primary"], th["primary_hover"], "#ffffff"),
            "ghost": (th["bg"], th["chip"], th["muted"]),
        }[kind]
        lb = tk.Label(parent, text=text, bg=bg, fg=fg, padx=10, pady=4, cursor="hand2",
                      font=self.f_bold if kind == "primary" else self.f_ui)
        lb.bind("<Button-1>", lambda e: self.root.after_idle(cmd))
        lb.bind("<Enter>", lambda e: lb.configure(bg=hover))
        lb.bind("<Leave>", lambda e: lb.configure(bg=bg))
        return lb

    def point_in_card(self, x, y) -> bool:
        r = self.card_rect
        return bool(r and r[0] - 2 <= x < r[0] + r[2] + 2 and r[1] - 2 <= y < r[1] + r[3] + 2)

    # ---------------- placement
    def _drag_bind(self, *widgets):
        for w in widgets:
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<Button-3>", self._drag_reset)

    def _drag_start(self, e):
        self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _drag_move(self, e):
        self.user_pos = (e.x_root - self._drag[0], e.y_root - self._drag[1])
        self.place()

    def _drag_reset(self, e):
        self.user_pos = None
        self.place()

    def place(self):
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        ms = get_monitors(self.root)
        if self.user_pos:
            ax, ay = self.user_pos[0] + w // 2, self.user_pos[1] + h // 2
        else:
            if self._anchor is None:
                self._anchor = self.root.winfo_pointerxy()
            ax, ay = self._anchor
        mx, my, mw, mh = monitor_at(ms, ax, ay)
        if self.user_pos:
            x, y = self.user_pos
        else:
            x, y = mx + mw - w - CARD_MARGIN, my + mh - h - BOTTOM_MARGIN
        x = max(mx + 4, min(x, mx + mw - w - 4))
        y = max(my + 4, min(y, my + mh - h - 4))
        if self.user_pos:
            self.user_pos = (x, y)
        self.win.geometry(f"+{int(x)}+{int(y)}")
        self.card_rect = (int(x), int(y), int(w), int(h))

    def _show(self):
        if self.win.state() == "withdrawn":
            self._anchor = None
            self.win.deiconify()
        self.win.lift()
        self.place()

    def _hide(self):
        if self.win.state() != "withdrawn":
            self.win.withdraw()
        self.card_rect = None
        self._anchor = None

    # ---------------- toasts / clipboard / collapse
    def show_toast(self, text):
        self.toast_text, self.toast_until = text, time.time() + 2.6
        self.render(self.model.snapshot(), force=True)

    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        if self.snap:
            self.render(self.snap, force=True)

    def toggle_clipboard(self):
        if self.model.mode == "clipboard":
            self.model.exit_static()
            return
        try:
            txt = self.root.clipboard_get()
        except Exception:
            txt = ""
        self.model.load_static(txt)

    # ---------------- explicit field scan (for apps whose text can't be read directly)
    def _on_ui(self, fn, timeout=2.0):
        """Run fn on the Tk thread from another thread and return its result."""
        done, box = threading.Event(), []

        def run():
            try:
                box.append(fn())
            except Exception:
                box.append(None)
            done.set()
        self.q.put(("call", run))
        done.wait(timeout)
        return box[0] if box else None

    def _clip_get(self):
        try:
            return self.root.clipboard_get()
        except Exception:
            return None

    def _clip_set(self, text):
        self.root.clipboard_clear()
        if text:
            self.root.clipboard_append(text)
        self.root.update()
        return True

    def scan_field(self):
        m = self.model
        if m.mode != "live" or m.app_ignored or m.paused:
            self.show_toast("Can't scan here (paused, ignored app, or clipboard mode)")
            return
        threading.Thread(target=self._scan_thread, daemon=True).start()

    def _scan_thread(self):
        m, K = self.model, self.model.K
        end = time.time() + 1.5
        while m.mods and time.time() < end:
            time.sleep(0.02)
        if m.mods:
            m.toast("Release the modifier keys, then try again")
            return
        if not m.inject_lock.acquire(blocking=False):
            return
        old = self._on_ui(self._clip_get)
        marker = f"\u200bwa-probe-{time.time()}"
        got = None
        try:
            self._on_ui(lambda: self._clip_set(marker))
            m.injecting = True
            with m.kb.pressed(K.ctrl):
                m.kb.tap("a")
            time.sleep(0.12)
            with m.kb.pressed(K.ctrl):
                m.kb.tap("c")
            time.sleep(0.25)
            got = self._on_ui(self._clip_get)
            m.kb.tap(K.right)                       # collapse the selection: caret goes to the end
            time.sleep(0.08)
        except Exception as ex:
            dbg("scan failed:", repr(ex))
        finally:
            m.injecting = False
            m.inject_until = time.time() + 0.5
            m.inject_lock.release()
            self._on_ui(lambda: self._clip_set(old))
        if got is None or got == marker:
            m.toast("Couldn't read this field")
            return
        m.adopt_probe(got)

    def copy_fixed(self):
        txt = self.model.static_text()
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(txt)
            self.root.update()
            self.show_toast("Copied to the clipboard")
        except Exception:
            self.show_toast("Couldn't copy to the clipboard")

    def close_card(self):
        if self.model.mode == "clipboard":
            self.model.exit_static()
        else:
            self.model.reset()

    # ---------------- rendering
    def render(self, snap, force=False):
        self.snap = snap
        now = time.time()
        toast_on = now < self.toast_until
        issues = snap["issues"]
        static = snap["mode"] == "clipboard"
        show = static or toast_on or (bool(issues) and not snap["paused"] and not snap["app_ignored"])
        if not show:
            self._sig = None
            self._hide()
            self.draw_underlines()
            return
        sel_key = snap["selected"]
        sig = (static, sel_key, self.collapsed, toast_on, snap["paused"],
               tuple((i["key"], i["hs"], i["he"], tuple(i["sugg"]), i["message"]) for i in issues),
               len(snap["text"]) if static else snap["text"][-PREVIEW_CHARS:])
        visible = self.win.state() != "withdrawn"
        if sig == self._sig and visible and not force:
            self.draw_underlines()
            return
        if not force and visible and now - self._rendered_at < 0.2:
            self._pending = snap                       # rebuild at most ~5x/second while typing
            self.draw_underlines()
            return
        self._sig, self._rendered_at, self._pending = sig, now, None
        for w in self.frame.winfo_children():
            w.destroy()
        if not issues and not static:
            self.label(self.frame, "✓  " + (self.toast_text or "All clear"), self.f_title, self.th["good"]).pack()
        else:
            self._build(snap, toast_on)
        self._show()
        self.draw_underlines()

    def _build(self, snap, toast_on):
        th, f = self.th, self.frame
        issues, static = snap["issues"], snap["mode"] == "clipboard"
        sel = next((i for i in issues if i["key"] == snap["selected"]), issues[0] if issues else None)

        hdr = tk.Frame(f, bg=th["bg"])
        hdr.pack(fill="x")
        title = self.label(hdr, "✎  " + ("Clipboard check" if static else "Writing Assistant"), self.f_title)
        title.pack(side="left")
        n = len(issues)
        if n:
            badge = tk.Label(hdr, text=f" {n} ", bg=KIND_COLOR.get(sel["kind"], "#ef4444") if sel else "#ef4444",
                             fg="#ffffff", font=self.f_bold)
            badge.pack(side="left", padx=(8, 0))
        else:
            badge = None
        self.button(hdr, "✕", self.close_card, "ghost").pack(side="right")
        self.button(hdr, "+" if self.collapsed else "–", self.toggle_collapse, "ghost").pack(side="right")
        if not static:
            self.button(hdr, "▶" if snap["paused"] else "⏸", self.model.toggle_pause, "ghost").pack(side="right")
        self._drag_bind(hdr, title, *([badge] if badge else []))
        if toast_on:
            self.label(f, self.toast_text, self.f_small, th["good"]).pack(anchor="w", pady=(4, 0))
        if self.collapsed:
            return
        if not sel:
            self.label(f, "No issues found — nice writing.", self.f_ui, th["good"]).pack(anchor="w", pady=(10, 4))
            if static:
                acts = tk.Frame(f, bg=th["bg"])
                acts.pack(anchor="w", pady=(6, 0))
                self.button(acts, "Copy text", self.copy_fixed, "primary").pack(side="left")
            return

        self._preview(f, snap, issues, sel, static)

        color = KIND_COLOR.get(sel["kind"], "#ef4444")
        meta = tk.Frame(f, bg=th["bg"])
        meta.pack(fill="x", pady=(10, 0))
        tk.Label(meta, text=f" {KIND_LABEL.get(sel['kind'], 'ISSUE')} ", bg=color, fg="#ffffff",
                 font=self.f_tiny).pack(side="left")
        self.label(meta, sel["message"], self.f_small, th["muted"], wraplength=300).pack(side="left", padx=(8, 0))

        word = sel["word"].strip() or "␠"
        top = sel["sugg"][0] if sel["sugg"] else None
        line = tk.Frame(f, bg=th["bg"])
        line.pack(fill="x", pady=(6, 0))
        if len(word) > 40:
            word = word[:38] + "…"
        self.label(line, word, (self.f_big[0], 13, "overstrike"), th["muted"]).pack(side="left")
        if top is not None:
            self.label(line, "  →  ", self.f_big, th["muted"]).pack(side="left")
            self.label(line, (top if top.strip() else "(remove)")[:40], self.f_big, th["good"]).pack(side="left")

        row = tk.Frame(f, bg=th["bg"])
        row.pack(anchor="w", pady=(8, 0))
        if sel["sugg"]:
            for idx, s in enumerate(sel["sugg"][:3]):
                txt = s if s.strip() else "remove"
                self.button(row, f"{idx + 1}   {txt[:28]}",
                            lambda idx=idx, k=sel["key"]: self.model.apply(k, idx),
                            "primary" if idx == 0 else "chip").pack(side="left", padx=(0, 6))
        else:
            self.label(row, "No automatic fix — rewrite it yourself, or dismiss.", self.f_small, th["muted"]).pack()

        acts = tk.Frame(f, bg=th["bg"])
        acts.pack(fill="x", pady=(8, 0))
        self.button(acts, "Ignore", self.model.ignore, "ghost").pack(side="left")
        if sel["kind"] == "spelling":
            self.button(acts, "Add to dictionary", self.model.add_word, "ghost").pack(side="left")
        if len(issues) > 1:
            pos = [i["key"] for i in issues].index(sel["key"]) + 1
            self.button(acts, "‹", lambda: self.model.step(-1), "ghost").pack(side="left", padx=(8, 0))
            self.label(acts, f"{pos}/{len(issues)}", self.f_small, th["muted"]).pack(side="left")
            self.button(acts, "›", lambda: self.model.step(1), "ghost").pack(side="left")
        auto = sum(1 for i in issues if i["auto"] and i["sugg"])
        if auto > 1 or (static and auto):
            self.button(acts, f"Fix all ({auto})", self.model.fix_all, "chip").pack(side="right")
        if static:
            self.button(acts, "Copy text", self.copy_fixed, "chip").pack(side="right", padx=(0, 6))
        self.label(f, self.hint, self.f_tiny, th["muted"], wraplength=360).pack(anchor="w", pady=(8, 0))

    def _preview(self, f, snap, issues, sel, static):
        th, text = self.th, snap["text"]
        if static:
            base = max(0, sel["hs"] - 110)
            end = min(len(text), base + 300)
        else:
            end = len(text)
            base = max(0, end - PREVIEW_CHARS)
            if sel["hs"] < base:
                base = max(0, sel["hs"] - 30)
                end = min(end, base + 260)
        shown = text[base:end].replace("\n", "↵")
        t = tk.Text(f, width=46, height=4 if static else 3, wrap="word", bg=th["panel"], fg=th["fg"],
                    bd=0, highlightthickness=0, padx=8, pady=6, font=(self.f_ui[0], 11), cursor="arrow")
        t.insert("1.0", shown)
        for i in issues:
            a, b = max(i["hs"], base) - base, min(i["he"], end) - base
            if b <= a:
                continue
            tag = "i" + str(abs(hash(i["key"])))
            t.tag_add(tag, f"1.0+{a}c", f"1.0+{b}c")
            t.tag_config(tag, underline=True, foreground=KIND_COLOR.get(i["kind"], "#ef4444"),
                         background=th["sel"] if i["key"] == sel["key"] else th["panel"])
            t.tag_bind(tag, "<Button-1>", lambda e, k=i["key"]: self.model.select(k))
            t.tag_bind(tag, "<Enter>", lambda e: t.config(cursor="hand2"))
            t.tag_bind(tag, "<Leave>", lambda e: t.config(cursor="arrow"))
        if not static:
            t.see("end")
        t.config(state="disabled")
        t.pack(fill="x", pady=(10, 0))

    # ---------------- in-place underlines
    def draw_underlines(self):
        if not self.lines:
            return
        want = []
        snap = self.snap
        if snap and snap["mode"] == "live" and not snap["paused"] and not snap["app_ignored"]:
            ox, oy = window_origin()
            ms = get_monitors(self.root)
            for i in snap["issues"]:
                if not i["rect"]:
                    continue
                x, y, w = i["rect"]
                x += ox - i["origin"][0]
                y += oy - i["origin"][1] + 1
                if any(m[0] <= x < m[0] + m[2] and m[1] <= y < m[1] + m[3] for m in ms):
                    want.append((int(x), int(y), max(int(w), 6), KIND_COLOR.get(i["kind"], "#ef4444"),
                                 4 if i["key"] == snap["selected"] else 3))
        want.sort(key=lambda it: (it[1], it[0]))
        want = want[:len(self.lines)]
        for idx, w in enumerate(self.lines):
            try:
                spec = want[idx] if idx < len(want) else None
                state = self._line_state.get(idx)
                if spec is None:
                    if state is not None:
                        w.withdraw()
                        self._line_state[idx] = None
                    continue
                if state != spec:
                    x, y, wd, color, th = spec
                    w.configure(bg=color)
                    w.geometry(f"{wd}x{th}+{x}+{y}")          # geometry BEFORE mapping: WMs ignore 1x1 maps
                    if state is None:
                        w.deiconify()
                        w.lift()
                    self._line_state[idx] = spec
            except tk.TclError:
                self._line_state[idx] = None

    def tick(self):
        try:
            self.draw_underlines()
            now = time.time()
            if self.toast_until and now > self.toast_until:
                self.toast_until = 0.0
                self.render(self.model.snapshot(), force=True)
            elif self._pending is not None and now - self._rendered_at >= 0.2:
                self.render(self._pending, force=True)
        except Exception as ex:
            dbg("tick error:", repr(ex))
        self.root.after(250, self.tick)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def init_runtime() -> None:
    CFG.clear()
    CFG.update(load_config())
    ensure_dictionary_folder()
    LEX.load_big()
    LEX.set_user_words(load_all_dictionaries())


def run_check(argv) -> int:
    """`--check "some text"` (or text on stdin): print the issues, no GUI needed."""
    rest = [a for a in argv[argv.index("--check") + 1:] if not a.startswith("--")]
    text = " ".join(rest) if rest else sys.stdin.read()
    init_runtime()
    issues = check_text(text, start_is_sentence=True)
    print(f"[{LEX.source}]")
    if not issues:
        print("No issues found.")
    for i in issues:
        sug = " | ".join(repr(s) for s in i.sugg[:3]) or "(no automatic fix)"
        print(f"{i.kind:9} {i.hs:4}-{i.he:<4} {i.word!r:28} {i.message}  ->  {sug}"
              + ("  [auto]" if i.auto else ""))
    return 0


def main() -> int:
    argv = sys.argv
    if "--version" in argv:
        print("Writing Assistant", VERSION)
        return 0
    if "--import" in argv:
        paths = [p for p in argv[argv.index("--import") + 1:] if not p.startswith("--")]
        print("Importing dictionaries...")
        import_dictionaries(paths)
        print("Done. They load automatically (within seconds if the assistant is running).")
        return 0
    if "--check" in argv:
        return run_check(argv)
    if tk is None or keyboard is None or mouse is None:
        print("Missing GUI dependencies. Install with:  pip install pynput pyspellchecker\n"
              "(On Linux you also need Tk: e.g. sudo apt install python3-tk, and an X11 session.)\n"
              "You can still try the checker without a GUI:  python writing_assistant.py --check \"teh text\"")
        return 1
    if sys.platform.startswith("linux") and os.environ.get("XDG_SESSION_TYPE") == "wayland":
        print("[wa] Wayland session detected: global key capture only works for XWayland apps. "
              "Log in with an X11 session for full support.")

    make_dpi_aware()
    init_runtime()
    import types
    Key = keyboard.Key
    root = tk.Tk()
    root.withdraw()
    ui_q: queue.Queue = queue.Queue()
    backend = make_field_backend()
    model = Assistant(lambda: ui_q.put(("refresh",)), lambda t: ui_q.put(("toast", t)),
                      keyboard.Controller(),
                      types.SimpleNamespace(left=Key.left, right=Key.right, backspace=Key.backspace,
                                            ctrl=Key.cmd if MAC else Key.ctrl),
                      CaretLocator(), backend=backend)
    ui = UI(root, model, ui_q)
    keys = Keys(model, ui_q)
    keys.ui = ui

    kw = {}
    if WIN and CFG["windows_filter_injected"]:
        def injected_filter(msg, data):
            try:
                if data.flags & 0x10:                  # LLKHF_INJECTED
                    if not (model.injecting or time.time() < model.inject_until):
                        model.reset()                  # another program typed: we can't follow it
                    return False
            except Exception:
                pass
            return True
        kw["win32_event_filter"] = injected_filter
    kb_listener = keyboard.Listener(on_press=keys.on_press, on_release=keys.on_release, **kw)
    ms_listener = mouse.Listener(on_click=keys.on_click, on_scroll=keys.on_scroll)
    stop = threading.Event()

    def shutdown():
        stop.set()
        model.stop.set()
        for lst in (kb_listener, ms_listener):
            try:
                lst.stop()
            except Exception:
                pass
        with model.lock:
            model.text, model.issues = "", []          # drop the typed-text buffer
        try:
            root.destroy()
        except Exception:
            pass

    def poll():
        refresh, toasts, actions, quit_ = False, [], [], False
        try:
            while True:
                msg = ui_q.get_nowait()
                if msg[0] == "refresh":
                    refresh = True
                elif msg[0] == "toast":
                    toasts.append(msg[1])
                elif msg[0] in ("collapse", "clipboard", "scan"):
                    actions.append(msg[0])
                elif msg[0] == "call":
                    try:
                        msg[1]()
                    except Exception as ex:
                        dbg("ui call failed:", repr(ex))
                elif msg[0] == "quit":
                    quit_ = True
        except queue.Empty:
            pass
        if quit_:
            shutdown()
            return
        try:
            for a in actions:
                {"collapse": ui.toggle_collapse, "clipboard": ui.toggle_clipboard, "scan": ui.scan_field}[a]()
            if toasts:
                ui.show_toast(toasts[-1])
            elif refresh:
                ui.render(model.snapshot())
        except Exception as ex:
            dbg("poll error:", repr(ex))
        root.after(50, poll)

    mods = "+".join(m.capitalize() for m in CFG["hotkey_modifiers"])
    print(f"Writing Assistant {VERSION} running.  Spelling: {LEX.source}")
    if not LEX.full:
        print("  Tip: pip install pyspellchecker  for full spell-checking "
              "(without it only well-known misspellings are flagged).")
    print(f"  Hotkeys ({mods} +): 1/2/3 apply · N/B next/prev · D ignore · W add word · F fix all · "
          f"A scan field · V check clipboard · C collapse · P pause · O dictionary folder · Q quit")
    if backend is None:
        print("  Field reading: off (typed-text tracking only).")
    else:
        extra = "" if not WIN or getattr(backend, "_auto", None) else "  (pip install uiautomation to also read browsers/Word)"
        print("  Field reading: on - checks the whole text field you are in." + extra)
    print(f"  Dictionary folder: {DICT_DIR}\n  Settings:          {CONFIG_PATH}")
    if MAC:
        print("  macOS: grant Accessibility + Input Monitoring to your terminal/Python in System Settings.")
    model.start()
    kb_listener.start()
    ms_listener.start()
    threading.Thread(target=watch_focus, args=(model, stop), daemon=True).start()
    root.after(50, poll)
    root.after(250, ui.tick)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
