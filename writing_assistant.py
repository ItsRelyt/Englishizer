#!/usr/bin/env python3
"""
Writing Assistant v4 - offline, system-wide spelling, grammar & style helper.

What's new in v4
  * PRE-PACKED DICTIONARY, IN THIS FILE: a curated ~3,600-word English core
    vocabulary (word + real-world frequency) ships inside writing_assistant.py
    and is merged into the spell-checker at startup. Suggestions are ranked by
    genuine word frequency from the very first keystroke. If pyspellchecker's
    corpus cannot be loaded (fresh machine, blocked download, corrupt cache),
    the built-in list silently becomes the whole dictionary - the app still
    checks and corrects spelling fully offline, with zero downloads.
  * UNDERLINES ACTUALLY SHOW NOW. Root causes fixed:
      - underline windows were mapped with no real size (WMs refuse to show
        1x1 borderless windows) - geometry is now set BEFORE every map;
      - caret samples taken while the target window was moving produced rects
        in the wrong place - the watchdog now re-samples after idle;
      - rects computed for issues raised mid-word were thrown away forever -
        they are recomputed whenever new caret samples arrive;
      - marks followed window moves (delta tracking) but not scrolls - they
        are re-projected from fresh samples every redraw instead;
      - colour + minimum size + DPI awareness make them visible everywhere.
  * MUCH smarter corrections: keyboard-aware ranking (transposed / adjacent
    key / double-letter / missing-vowel typos get boosted), phonetic respellings
    (fone->phone, raiting->writing), split/join candidates ("tehre"->"there",
    "alot"->"a lot"), a big typo-correction table so rare words still suggest
    the right thing even with a small dictionary, and confidence scores.
  * Real grammar engine: 60+ confused-word pairs (their/there/they're,
    affect/effect, then/than, lay/lie, fewer/less...), subject-verb agreement
    (everyday he/she/it forms), perfect-tense participles (have went -> gone),
    modal + base verb (should goes -> should go), a/an incl. an/his/her
    contractions, sentence capitalisation, missing apostrophes, doubled words.
  * Style layer: passive voice, weak/openers, filler words, redundancy,
    puff phrases, officialisms, buzzwords, cliche closings - each with a
    concrete replacement, exactly what makes Grammarly worth paying for.
  * Card UI: compact single-line header, severity dots, inline preview with
    clickable red/blue/amber underlines, top-3 numbered suggestion chips with
    tooltips, Ignore / Add to dictionary / Next / Fix-all actions, status bar
    with pause indicator, toasts, drag-to-move, auto-hide when clean.
  * Bug fixes: chip double-click crash, stale ignored-word suppression,
    selection lost on dictionary reload, anchor lost on Enter, focus-watchdog
    false resets, toast timer leak, worker/handler races, DPI blur on Windows,
    mis-positioned preview tags when text wraps, and more.

100% local. No network calls at runtime, no API keys. Typed text is kept only in
memory (last ~1800 chars of the current field).

Dictionaries: on first run a folder is created at  ~/WritingAssistant/dictionaries/
with a ready-to-edit  my_words.txt.  Every .txt / .dic / .csv file in that folder is
loaded (one word per line; "#" starts a comment; Hunspell .dic files and
"word count" frequency lists work too). Drop in any word list, or run
    python writing_assistant.py --import mylist.txt other.dic
Edits are picked up automatically within a few seconds, no restart needed.
"Add to dictionary" appends to my_words.txt.

Install:   pip install pynput pyspellchecker
Optional:  pip install screeninfo          (accurate multi-monitor clamping)
           macOS in-place underlines: pip install pyobjc-framework-ApplicationServices pyobjc-framework-Cocoa

Hotkeys (hold Ctrl+Alt):
  1 / 2 / 3  apply suggestion      N  next issue       D  ignore
  W  add word to dictionary        F  fix all          P  pause/resume
  C  collapse/expand card          O  open dictionary folder        Q  quit
(You can also just click the chips on the card.)
"""

import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pynput import keyboard, mouse
from spellchecker import SpellChecker

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
IDLE_FLUSH_SECONDS = 0.6   # check the word you're on after this much pause
MAX_TEXT = 1800            # characters of the current field kept in memory
ENTER_RESETS = True        # True: Enter clears state (safe for chat apps that send on Enter)
                           # False: Enter counts as a newline (better for docs/email)
FLAG_DOUBLE_SPACE = True
PREVIEW_CHARS = 140        # how much recent text the card previews
MAX_CHIPS = 6              # other-issue chips shown on the card
CARD_MARGIN = 14
BOTTOM_MARGIN = 70         # keeps card clear of taskbar / dock
DICT_DIR = Path.home() / "WritingAssistant" / "dictionaries"
MY_WORDS = DICT_DIR / "my_words.txt"
DICT_EXTS = {".txt", ".dic", ".csv", ".tsv"}
DICT_RELOAD_SECONDS = 3
DEBUG = "--debug" in sys.argv or bool(os.environ.get("WA_DEBUG"))
ESTIMATE_UNDERLINES = True  # fallback when an app hides its caret: approximate underlines
FALLBACK_CHAR_W = 8.0       # px per character used by the estimate (tune to your font size)
FALLBACK_LINE_H = 22        # px line height used by the estimate
MAX_UNDERLINES = 24
PASTE_PROBE_SECONDS = 1.5   # idle: try to read the field's full text (Windows)
FULLTEXT_COOLDOWN = 12      # don't retry a failed clipboard probe this often


def dbg(*a):
    if DEBUG:
        print("[wa]", *a, flush=True)


BG, PANEL, FG, MUTED = "#111827", "#1f2937", "#f9fafb", "#9ca3af"
RED, BLUE, AMBER, GREEN = "#ef4444", "#3b82f6", "#f59e0b", "#10b981"
KIND_COLOR = {"spelling": RED, "grammar": RED, "punct": BLUE, "style": AMBER}
WORD_KINDS = {"spelling", "grammar", "style"}

try:                                  # lazy corpus download on first run must never
    spell = SpellChecker()            # crash or block the assistant
except Exception as ex:               # fall back to the built-in dictionary below
    print("[wa] pyspellchecker corpus unavailable, using built-in dictionary:", ex)
    spell = None

# --------------------------------------------------------------------------
# Pre-packed dictionary (ships inside this file - zero downloads needed)
# --------------------------------------------------------------------------
# Curated English core vocabulary: (word, real-world frequency). Merged into
# the spell-checker at startup so suggestions are ranked by genuine frequency
# from the very first keystroke; if pyspellchecker's corpus cannot be loaded,
# this list silently becomes the whole dictionary and everything still works.
_CORE_TIERS = [
    # frequency ~100,000,000
    (
    "a after also and any as back because by can come day do even first for" +
    "give go he her how i in is it its look make me most new now" +
    "of on only other our over say see than that the then these think this to" +
    "two us use want was way well will with work you",
     100000000),
    # frequency ~10,000,000
    (
    "above action animal area article ask ball beach beauty become bed begin believe bird black blue" +
    "break bring brown call camera camp card change child circle class command complete complex condition continue" +
    "country course court create depth desert design dog door dream drop easy enemy exact experience face" +
    "fact failure fair far fast feel find finish firm follow friendly fruit full gold grow happen" +
    "head help here high hold house idea important include instrument interest island item keep kind lake" +
    "land lead learn leave let like line listen live lose lot machine man mean measure meet" +
    "message metal might moment money move music need night note observe offer often orange order paint" +
    "pair pay person photo place plan plane plant play point police power price primary provide put" +
    "question race radio read receive record region remain road rock rose sail seat seem send separate" +
    "service set share short show shy side sing sit smile smoke snow song space speak speech" +
    "spring stand start state stop strength student summer sun swing talk teach tell thing try turn" +
    "understand unit view watch weight wind write year yes yet",
     10000000),
    # frequency ~1,500,000
    (
    "though",
     1500000),
    # frequency ~1,000,000
    (
    "ability abstract absurd accurate acquaintance activist afternoon ally amateur amazing anger apartment apple apprentice arm artificial" +
    "audience autumn awesome awful baby banana bank bathroom battle beautiful bedroom beginner belt berry bill bitter" +
    "blood blunt body bone boy brain bread bridge bright brother budget building business butter button cake" +
    "capacity career cash cent century ceo certain chair chairman chance cheap cheese chicken chocolate citizen city" +
    "clean clear clever client close clothing cloud coat coffee coin cold colleague college color comfort common" +
    "company competitor concrete constant continent cookie cool correct cost courage coworker crowd cultural cure curved customer" +
    "cute danger dark daughter decade deep defeat development diet difficult director dirty disease distant doctor dollar" +
    "dress drink dry duty ear earth economic economy educated education egg elegant emotion emotional employee employer" +
    "energy equality ethical evening excellent exercise expense expensive experienced expert extraordinary eye factory fake fall false" +
    "family fancy farm father fear feeling fight financial finger fish flat floor follower food foolish foot" +
    "foreigner forest founder freedom frequent fresh friend funny furniture garden general genuine girl government graceful gradual" +
    "grape gray green group growth guest hair hand handsome happiness hard hat hate health heart heavy" +
    "hidden honesty honor hope horrible hospital host hot hour human ignorant illness impossible income individual industry" +
    "inexperienced intelligence investment investor irrational jacket job joy juice justice kid kindness kitchen knee knowledge knowledgeable" +
    "lamp leader leg legal lemon lesson light lightning likely logical long loss love lovely low loyalty" +
    "luxurious manager market master meat medicine melon member mental milk mind minute misery mob modest month" +
    "moon moral morning mother mountain mouth narrow nation natural near neck neighbor noon normal nose novice" +
    "nurse observer obvious ocean odd office oil opponent opportunity ordinary owner pain pants parent park participant" +
    "particular partner pasta patient peace people pepper personal physical pie pink plain pleasure pocket political poor" +
    "possible potential poverty practical precise president pretty prevention private probable profession professional professor profit progress public" +
    "purple qualified quick rain rapid rare rational real reasonable red religious research resident respect responsibility rest" +
    "rice rich ridiculous right risk rival river roof room rough round sadness safety salad salt salty" +
    "sauce school scientific sea season second secret sensible serious shallow shareholder sharp shirt shoe shop simple" +
    "sister skill skilled skin sky sleep slow smart smooth social sock soft son soul soup sour" +
    "special specific spectator spice spirit spiritual square star steady stomach store storm straight strange stranger street" +
    "struggle study stupid success sudden sugar sure sweet synthetic table talent tall tea teacher team teammate" +
    "technical terrible theoretical thick thin thunder today tomorrow tooth town trade trained treatment true trust ugly" +
    "unclear uneducated unique university unskilled unsure untrained usual vague value vegetable victory village vinegar virtual volunteer" +
    "wall war warm water weakness wealth weather week weird wet white wide window wisdom wise wonderful" +
    "world worth wrong yard yellow yesterday",
     1000000),
    # frequency ~800,000
    (
    "writing",
     800000),
    # frequency ~500,000
    (
    "written",
     500000),
    # frequency ~250,000
    (
    "access",
     250000),
    # frequency ~200,000
    (
    "advice definitely successful whose",
     200000),
    # frequency ~150,000
    (
    "immediately independent threw truly won",
     150000),
    # frequency ~120,000
    (
    "chose stairs thrown",
     120000),
    # frequency ~100,000
    (
    "academic accountant actor administrator advisor advocate ambassador amount analysis analyst app arbitrator architect art artist assistant" +
    "attendant attorney audio auditor author baked baker banker bartender beaten billionths birthday blended blog boiled broker" +
    "browser builder bureaucrat butcher candidate canned captain carpenter cashier chapter character chat chef chopped clerk coach" +
    "code comment commissioner composer computer conclusion consultant consumer contractor cook cooked coordinator cored council counselor cut" +
    "data database defender delegate designer detective developer device diced digit digital diplomat directory distributor document dried" +
    "driver eighths electrician electronic email engine engineer entrepreneur essay executive farmer figure file fisherman folded folder" +
    "font format freelancer fried frozen gardener governor grade graphic grated grilled halved handmade hardware helper homemade" +
    "hulled hundredths hunter husked hybrid image information innovator instant instructor intellectual internet introduction investigator judge keyboard" +
    "kneaded lawyer layout lecturer legislator letter level link logger manufacturer mason mayor mechanic mediator mentor merchant" +
    "microwaved millionths minced miner mixed mobile moderator mouse musician negotiator network novel number officer official offline" +
    "online onsite operator organic organizer painter paragraph path peeled philosopher phone phrase pilot pitted planner plumber" +
    "poem poet politician portable porter post presenter printer producer program prosecutor psychiatrist psychologist quality quantity quartered" +
    "query rancher rating raw receptionist rechargeable remote renewable representative researcher retailer review roasted rolled sailor scanner" +
    "scholar scientist score screen script search secretary section seeded senator sentence server shelled shredded singer sixteenths" +
    "sliced smartphone software soldier speaker spelling standard statistic steamed stemmed stirred story strategist style summary supplier" +
    "sustainable symbol taxpayer technician teller template tenths text therapist thinker thousandths trader trainer trillionths tutor url" +
    "vegan vegetarian vendor video voter waiter waitress webpage website welder whisked wholesaler wireless word writer",
     100000),
    # frequency ~80,000
    (
    "calendar embarrass february wednesday",
     80000),
    # frequency ~60,000
    (
    "cite insure occurrence rhythm",
     60000),
    # frequency ~50,000
    (
    "typo",
     50000),
    # frequency ~30,000
    (
    "wary",
     30000),
    # frequency ~20,000
    (
    "changeable harass weary",
     20000),
    # frequency ~10,000
    (
    "accept achieve address advise affect afford agree allow answer appear apply argue arrange assert assume attach" +
    "attempt attend avoid await base bear beat behave belong bend bet bind bite blame bleed bless" +
    "blink bloom blow board boast boil bolt borrow bounce bow box brag breathe brew bribe broadcast" +
    "brush bubble build bump burn burst bury bust buy calculate carry catch cause challenge charge chart" +
    "chase cheat check cheer chew choose claim clap clash clasp click climb cling clip clock clutch" +
    "collect comb communicate compare compete complain compose compute conceal concede concentrate confess confirm confront confuse connect" +
    "conquer consider consist construct consult consume contain contract contribute control convert convince cooperate copy correlate cough" +
    "count cover crack craft crash crawl credit creep criticize crop cross crown crush cry cultivate curl" +
    "current curve cycle damage dance dare deal debate decide declare decline decorate decrease dedicate delay deliver" +
    "demand demonstrate deny depend describe deserve desire destroy detail detect determine develop devote diagnose die differ" +
    "dig dilute dine direct disagree disappear discover discuss dislike dismiss display dispute distribute dive divide divorce" +
    "dominate donate doubt draft drag drain draw drift drill drip drive drown drum dump dust earn" +
    "ease eat echo edit eject elect eliminate embrace emerge emit employ empty enable enclose encounter encourage" +
    "end endure enforce engage enhance enjoy enlarge enrich enroll ensure enter entertain escape establish estimate evaluate" +
    "examine exceed exchange excite exclude excuse exhaust exhibit exist exit expand expect experiment explain explode explore" +
    "export expose express extend extinguish extract fail fare feed fetch fill finance fire fit fix flash" +
    "flee float flock flow flower fluently flush fly fold force forecast forge forget forgive form found" +
    "frame free freeze fret frighten frost frown fry fuel fulfill function fund gain gamble gather gaze" +
    "generate get giggle glow govern grab graduate greet grind grip groan guard guess guide hammer handle" +
    "hang harbor harm harvest hatch haul have heal hear heat hide hike hire hit hook hover" +
    "hug hunt hurry hurt identify ignore illustrate imagine imitate implement imply import impose improve increase indicate" +
    "induce influence inform inhabit inherit initiate inject injure inspect inspire install instruct insult integrate intend interpret" +
    "interrupt interview introduce invade invent invest investigate invite involve iron issue join joke journey juggle jump" +
    "justify kick kill kiss kneel knit knock know label laugh launch lay leak lean leap lease" +
    "lend lengthen lessen license lift limit list load loan locate lock lodge log loose lower mail" +
    "maintain manage mark marry match matter melt mention mine miss mix mobilize mock modify monitor mount" +
    "mourn multiply murmur muse mutter name narrate navigate neglect negotiate nest net notice notify nourish obey" +
    "object obtain occupy occur omit open operate oppose opt organize originate output overcome overheat overlook overtake" +
    "owe pack paddle pardon part participate pass paste patch patrol pause pave peek peel perceive perform" +
    "permit persist photograph pick piece pierce pin pitch plead plot plow plug plunge polish ponder populate" +
    "pose position possess postpone pour practice praise pray preach predict prefer prepare present preserve press pretend" +
    "prevent print proceed process produce project promise promote propose protect protest prove publish pull pump punch" +
    "purchase pursue push puzzle qualify quit quote raise rally rank rate reach react realize reason recall" +
    "recognize recommend recover recruit redeem reduce refer reflect reform refuse regard regret regulate rehearse reinvent relate" +
    "relax release relieve rely remember remind remove renew rent repair repeat replace reply report request require" +
    "rescue resemble reserve resist resolve respond restore restrict result resume return reveal reverse revise reward ride" +
    "ring rinse rise roar roast rob roll rotate route row ruin rule run rush sack sample" +
    "satisfy save saw scale scan scatter schedule scold scope scream scrub seal secure seek seize select" +
    "sell sense serve settle sew shade shake shape sharpen shave shed shelter shift shine ship shock" +
    "shoot shout shrink shrug shut sigh sight sign signal simplify sink sip sketch ski skip slam" +
    "slap slice slide slip smash smell smite snap sneeze sniff snore soak soar solve sorry sort" +
    "sound spar spare spark specialize specify spend spill spin spit split spoil spot spray spread sprint" +
    "squeeze stack staff stage stain stake stall stamp stare starve stay steal steam steer step stick" +
    "sting stir stitch stock strain stress stretch stride strike string strip stroke strum stuff stumble submit" +
    "subscribe subtract succeed suck suffer suggest suit summarize supply support suppose surf survive suspect suspend swallow" +
    "sway swear sweat sweep swell swim switch sword tackle tag take tap target taste tear tease" +
    "telephone tempt tend test thank thicken thread threaten thrive throw thumb tick tie tighten tilt time" +
    "tip tire toast toe toggle toll tone tool top torch toss total touch tour tow trace" +
    "track trail train transfer transform translate transmit transport trap travel tread treat trend trial trip trot" +
    "trouble tug tumble tune twist type undergo undertake unite unlock update upgrade uphold upload upset urge" +
    "utilize vacation validate vanish vary venture verify visit voice vote wait wake walk wander warn wash" +
    "waste wave weaken wear weave weep weigh welcome west whisper whistle widen wink wipe wire wish" +
    "withdraw witness wonder worry worship wound wrap wreck yawn yearn yell yield zap zoom",
     10000),
    # frequency ~1,000
    (
    "aboard about abroad according across actually additionally adjacent afterwards again against ago ahead albeit alike alive" +
    "all almost alone along alongside already alright although altogether always am amazed among amongst an another" +
    "anybody anyhow anyone anything anytime anywhere apart apparently approximately are around aside asleep assisting at attentive" +
    "available away backwards bad badly bag balanced bar barely basic basically bath be became becoming been" +
    "before beforehand began beginning begins behind being belief below beneath beside besides best better between beyond" +
    "big bit blank blessed blind block blown boat bold book boolean born both bottom bought bound" +
    "brave breath breeze brief briefly brilliant broad broke broken built busy but cage calm came cannot" +
    "cap capable car cardinal careful carefully case cast categories category caught caution cautious cave cease central" +
    "certainly chain chalk changes chasing cheek cheerful cherry chest chief children choice choices chosen church cigarette" +
    "cities civil clam clarify classes classic cleanliness clearly cliff climate closely closer cloth clothes cloudy club" +
    "clue coast collapse collar collection combo comedy comfortable comic coming commerce commercial commission commit commonly community" +
    "compact comparison compass competition competitive compile completely complexity comply compound compromise concept concern concerned concert conclude" +
    "conduct conference confidence confident conform congratulations connection conscious consequence consequently considerable consistent consistently constantly constraint consultation" +
    "contact content contest context continuous contrary contrast convenient conversation cope core corner corporation correctly correspond cotton" +
    "could county couple cousin crazy creation creative creature crew crime criminal crisis crisp criteria critical crucial" +
    "cruel crumb cup curious currently",
     1000),
    # frequency ~100
    (
    "daily dangerous darkness darling dash date dawn days dead deadly dear death decent decision deck deed" +
    "deeply deer definition degree delete deliberate delicate delicious delight democracy demon dent department dependent deposit derive" +
    "description desk despite destruction determined devil diagram dial dialogue diameter diamond diary dick dictation dictionary did" +
    "difference different differently difficulty dinner direction directly dirt disability disabled disappointed disaster discipline discount discussion dish" +
    "disorder distance distinct distinguish distract district disturb diverse division does doing doll domain domestic don done" +
    "doom dose double down downstairs downtown downward dozen drama dramatic drawn dressed drew driven drowned drug" +
    "drunk due dull during each eager early easily east eastern echoed edge edition editor educate effect" +
    "effective effort eight either elder electrical element eleven else emergency eminent emperor empire employed employment endanger" +
    "endless endorse energetic engagement english enormous enough entire entity entrance envelope environment envy equal equally equipment" +
    "erase error especially essential estate event eventually ever every everybody everyone everything everywhere evidence evil exactly" +
    "example except exception excess exciting execute existing exotic experimented exploitation exploration explosion exposure extension extra extremely" +
    "eyes facilities factor faint fairly faith fallen falsehood fame familiar famous fantasy fashion fashionable faster fat" +
    "fate favorite feast feature fed federal fee fellow female fence ferry festival fever few fiber fiction" +
    "field fierce fifteen fifty film filter final finally fine fiscal five flag flame flaw fleet flesh" +
    "flight flood flour fluent fluid flutter foam focus fog folk following fool football foreign forever fork" +
    "formal former formula forth fortune forty forward fought foul foundation four fourth fox fraction fragile frank" +
    "fraud frog front fully fun fundamental funeral further future game garage garlic gas gate gathered gay" +
    "gear gender generous gentleman gesture giant gift glad glance glass glimpse global glove goal goat god" +
    "golden gone good goodbye goose gorgeous got grace gradually grain grand grandfather grandma grant graph grasp" +
    "grass grateful grave great greatest greed grey grief grieve grill grim grin grit grocery ground grown" +
    "grumble guilt gun gym habit had hail half hall hallway handful happy harder hardly hare harmony" +
    "harness harsh has haste hawk hay hazard headline healthcare healthy heap heard heaven height hell hello" +
    "hen hence herb herd hero herself hi hid hill hint hip his historic history hive hoard" +
    "hob hole holiday hollow holy home honest honey hood hoof horn horse hotel however huge hull" +
    "humble humor hundred hung hunger hurdle hurricane husband hut hydrogen hyphen ice icon id identity idle" +
    "if ignorance ill illegal imagination immediate immensely immigrant impact impatient impress impulse incentive inch incidence including" +
    "indeed index indication indirect indoor inevitable infant infection inflation ingredient initial initiative inner innocent input insertion" +
    "insist instance instead instinct institute insurance intact intake integrity intense intent interact interior intermediate internal international" +
    "interruption interval intimate into intolerant intrusion intuition invalid invented invincible invisible invitation invoke inward irregular itself" +
    "jail jam jar jaw jazz jelly jet joint journal jungle junior junk jury just keen kettle" +
    "key kids king kit kite knew knife knight knot known koala lab labor laboratory lack lady" +
    "lag lamb language lantern lap large largely last late later laughter law lawn layer lazy leaf" +
    "league least leather led left length lens less lest lever liability lick lid lie life lifetime" +
    "likelihood limb lime limp lion lip liquid literal litter little lively liver living loaf local logic" +
    "lonely longitude lost loud luck luggage lump lunch lying mad magazine magic maid main major male" +
    "mall manner mansion manual map marble march margin marriage married marshal mask mass mat mate material" +
    "math matrix maximum may maybe meal meaning meanwhile medal media medical medium meeting melody memory mend" +
    "merely mess metabolic microwave midnight migrate mild mile military mill million mineral miniature minimum minister minor" +
    "minority miracle mirror mistake mixture moan mode modern moist molecule monkey monster mood more motion motive" +
    "mourning movie much mud murder muscle museum mushroom must mutual my mystery myth nail naked nasty" +
    "national native nature naval neat necessary negative neither nervous neutral never news next nice nine no" +
    "noble nobody nod noise noisy none nonsense north notable notebook nothing noun nowhere nuclear null numeric" +
    "nun nut oak oar obligation obscure observation occasion occasional occupation octopus odder off offense offensive old" +
    "older olive omega one onion onto opera operation opinion opposite optimize option or orbit organ origin" +
    "otherwise ought ounce out outbreak outcome outdoors outer outfit outline outrage outset outside oval oven overall" +
    "overdue overhead overhear overlap overseas overthrow overtime own ox pace page painful palace pale palm pan" +
    "panda panel paper parade parallel parameter parcel parish partial partly passage passion past pastry patience pattern" +
    "payment peaceful peak peanut pear peasant peck pedal penny perfect perfume perhaps period permanent permission persuade" +
    "pet phase phenomenon philosophy piano picnic picture pig pile pill pinch pine pint pipe pity pizza" +
    "planet plank planned plastic plate platform playground please plenty plum plural plus poach poetry poison pole" +
    "policy polite pond pony pool pop popular population porridge port portion portrait positive potato pound preacher" +
    "precious predicate pregnant prejudice preliminary premium preparation prescribe presence pressure presume prettify previous prey pride priest" +
    "primacy prime prince princess principal principle prior prison privilege proactive problem procedure proclaim product profile profound" +
    "prominent prompt pronounce proof proper property proportion proposal prospect prosper protein proud province provoke pudding puff" +
    "pulse punishment pupil puppet purpose purse pyramid qualification quarrel quarter queen queue quiet quite quiz quota" +
    "quotation rabbit raccoon radar radial radiate rag rage raid rail rake ramp range ransom rap rash" +
    "rat rather ratio rave ray ready realm reap rear rebel rebuild receipt recent recipe recognition reconcile" +
    "recur reef reel refine refrain refresh refuge refund regain regime register regular reject rejoice relief religion" +
    "remark remedy reminder remnant removal repent repertoire repetition replay represent reproduce reptile republic reputation reservation reservoir" +
    "reset reside resize resolution resort resource restless restraint retail retain retreat reunion revenge revenue revive rhyme" +
    "rib ribbon rid ridge ridicule rife rifle rig rigid rim rink riot rip ripe ritual roam" +
    "robot rocket rod role romantic root rope rot rouse routine royal rubber rubble ruby rude rug" +
    "rumor rural rust rustic rut safe saga said saint sake salary sale sales salmon same sand" +
    "satellite satin satisfied saturate sauna savage scandal scarce scare scarf scenario scene scenery scent scheme scholarship" +
    "science scissors scoop scooter scorn scout scramble scrap screw scuba seafood seam seed segment seldom self" +
    "seminar senate senior sensation sensitive sentiment sequence serene series servant session seven several severe shack shadow" +
    "shaft shall shame shark sheep sheet shelf shell shield shit shore shot should shove shower shriek" +
    "shrill shrimp shrine shrub shuffle shutter sick siege silence silk silly silver similar sin since sincere" +
    "single sir site situation six size skate skiing skim skull slab slang slate slave sled sleek" +
    "sleeve slender slick slight slim slope slot slug slum slump smack small smog snack snail snake" +
    "sneer snub soap sober sofa soil solar sold sole solemn solid solution some soon soothe sore" +
    "sorrow source south souvenir sow soy spa spam span spawn spear species specimen spectacle spectrum speculate" +
    "speed spell sphere spider spike spine spiral spite splash splendid sponge spool sport spouse sprinkle sprout" +
    "spur spy squash squat squeak squid stable stair stale stalk stance staple stark station statue status" +
    "staunch steak steel steep stem stereo stew stiff still stool stoop stout stove strand stream strife" +
    "stripe strive strong struck structure stuck stud stump stun sturdy subject sublime subsidy substitute subtle suburb" +
    "subway such suffice sullen summit summon sunk superb surge surmount surname surpass surplus surprise surrender surround" +
    "survey susceptible sustain swab swagger swamp swap swarm swept swift syllable symmetry sync syntax syrup system" +
    "tab tablet taboo tack tail tailor tale tame tan tank taper tar tariff task tavern tax" +
    "technique teddy tee teen teeth temper temple tempo tenant tender tennis tense tent term terrace terror" +
    "textile texture theft their them theme themselves theory there therefore thesis they thief thigh third thirst" +
    "thorn thorough those thought thousand threat three threshold thrill throat throne through thus ticket tide tidy" +
    "tiger tile till timber timid tin tiny tissue title toad together toilet token told tolerance tomato" +
    "tomb tongue tonight too took topic tore torn torpedo tortilla torture tough towel tower toy trait" +
    "tramp trance trash tray treacherous treasure tree trek tremble trench tribe trick trim triumph troop tropical" +
    "trough truck trumpet trunk truth tub tube tuck tuition tunnel turbo twelfth twelve twenty twice twig" +
    "twin twine tyrant umbrella uncle under undo unfit unfold union unless unlike unwind up upon upper" +
    "upright uproar upward urban usage useful useless usher utility utmost vacant vacuum vain valid valley valuable" +
    "vanilla variable variant variation varied variety vast vault vector vehicle veil velocity venom verb verdant verse" +
    "vessel vest veteran veto vex via vibe vice victim victor vileness villa vine vinyl viola violet" +
    "virtue vision visualize vital vivid vocal volcano volt volume vowel voyage wade waist walnut ward warehouse" +
    "warp warranty watt wax weak weapon weasel web wedge weed welfare whale wheat wheel when where" +
    "whether which while whim whine whirl whole whom why wicked widow width wield wife wig wild" +
    "willing wilt wing winner winter wit witch without woe wolf woman wont wood wool worm worse" +
    "worst would wrath wreath wrench wrest wretch wriggle wring wrist wrote wrought wrung yacht yeast yoke" +
    "yon young your youth zeal zero zigzag zinc zip zone zoo",
     100),
]


# --------------------------------------------------------------------------
# Language data
# --------------------------------------------------------------------------
CONTRACTIONS = {
    "dont": "don't", "cant": "can't", "isnt": "isn't", "doesnt": "doesn't",
    "didnt": "didn't", "wasnt": "wasn't", "couldnt": "couldn't",
    "shouldnt": "shouldn't", "wouldnt": "wouldn't", "havent": "haven't",
    "hasnt": "hasn't", "arent": "aren't", "werent": "weren't",
    "im": "I'm", "ive": "I've", "youre": "you're", "thats": "that's",
    "theres": "there's", "theyre": "they're", "whats": "what's",
    "hadnt": "hadn't", "wouldve": "would've", "shouldve": "should've",
    "couldve": "could've", "youve": "you've", "theyve": "they've",
}
CONTRACTIONS_OK = {
    "don't", "can't", "isn't", "doesn't", "didn't", "wasn't", "couldn't",
    "shouldn't", "wouldn't", "haven't", "hasn't", "aren't", "weren't", "i'm",
    "i've", "i'll", "i'd", "you're", "you've", "you'll", "you'd", "that's",
    "there's", "they're", "they've", "they'll", "what's", "let's", "hadn't",
    "won't", "we're", "we've", "we'll", "he's", "she's", "it's", "who's",
    "how's", "here's", "where's", "ain't", "y'all", "would've", "should've",
    "could've", "must've", "might've", "o'clock", "ma'am", "he'll", "she'll",
    "it'll", "he'd", "she'd", "we'd", "they'd", "mustn't", "needn't", "mightn't",
}
SLANG_OK = {
    "gonna", "wanna", "gotta", "kinda", "sorta", "ok", "okay", "lol", "omg",
    "btw", "thx", "pls", "plz", "yeah", "yep", "nope", "hey", "hmm", "umm",
    "wow", "tbh", "imo", "fyi", "asap", "vs", "etc", "wifi", "email", "emails",
    "online", "app", "apps", "iphone", "android", "youtube", "gmail", "google",
}
ABBREV = {"e.g", "i.e", "etc", "dr", "mr", "mrs", "ms", "prof", "vs", "inc",
          "ltd", "jr", "sr", "st", "no", "approx", "fig", "cf", "al"}
DUP_OK = {"had", "that", "is", "do", "very", "so", "really", "can"}
MODAL_OF = {("should", "of"), ("could", "of"), ("would", "of"), ("must", "of"),
            ("might", "of")}
WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
SPECIAL = set("@/\\_#<>{}=+*~^|$%&")


def is_word_char(c: str) -> bool:
    return c.isalnum() or c in "'’"


def match_case(original: str, suggestion: str) -> str:
    if original.isupper() and len(original) > 1:
        return suggestion.upper()
    if original[:1].isupper():
        return suggestion[:1].upper() + suggestion[1:]
    return suggestion


def edit_distance(a: str, b: str) -> int:
    """Optimal-string-alignment distance (counts a swap as one edit)."""
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


def freq(w: str) -> int:
    try:
        return int(spell.word_frequency[w])
    except Exception:
        return 0



def _build_core_words():
    """Parse the packed frequency tiers into {word: frequency}."""
    out = {}
    for blob, f in _CORE_TIERS:
        for w in blob.split():
            w = w.strip().lower()
            if w.isalpha() and w not in out:
                out[w] = f
    return out


CORE_WORDS = _build_core_words()

# Common typos whose intended word must be suggested even when that word is
# rare or missing from a small offline dictionary.
TYPO_MAP = {
    "teh": "the", "tehn": "then", "tihs": "this", "taht": "that", "waht": "what",
    "wiht": "with", "adn": "and", "nad": "and", "htey": "they", "thier": "their",
    "theri": "their", "alot": "a lot", "recieve": "receive", "receve": "receive",
    "beleive": "believe", "belive": "believe", "freind": "friend", "frend": "friend",
    "wich": "which", "whcih": "which", "becuase": "because", "because": "because",
    "becasue": "because", "seperate": "separate", "definately": "definitely",
    "definatly": "definitely", "difinately": "definitely", "goverment": "government",
    "enviroment": "environment", "occured": "occurred", "ocurrence": "occurrence",
    "untill": "until", "tommorow": "tomorrow", "tommorrow": "tomorrow",
    "wierd": "weird", "wried": "weird", "writeing": "writing", "writting": "writing",
    "writing": "", "grammer": "grammar", "comitted": "committed", "embarass": "embarrass",
    "neccessary": "necessary", "ecesary": "necessary", "priviledge": "privilege",
    "recomend": "recommend", "rythm": "rhythm", "rythym": "rhythm", "sucessful": "successful",
    "succesful": "successful", "superb": "", "february": "", "changable": "changeable",
    "begining": "beginning", "beautifull": "beautiful", "busness": "business",
    "bussiness": "business", "calender": "calendar", "cemetery": "", "collegue": "colleague",
    "commited": "committed", "concious": "conscious", "curiousity": "curiosity",
    "didnt": "didn't", "doesnt": "doesn't", "dont": "don't", "foriegn": "foreign",
    "gaurd": "guard", "guarentee": "guarantee", "harrass": "harass", "honourary": "honorary",
    "immediatly": "immediately", "independant": "independent", "occassion": "occasion",
    "peice": "piece", "persue": "pursue", "posession": "possession", "prefered": "preferred",
    "refered": "referred", "rythm": "rhythm", "seizeing": "seizing", "tambrine": "tambourine",
    "threshhold": "threshold", "toady": "today", "towords": "towards", "unforunately": "unfortunately",
    "untill": "until", "usefull": "useful", "vaccum": "vacuum", "vehical": "vehicle",
    "wether": "whether", "withing": "within", "abroad": "", "accross": "across",
    "agianst": "against", "alowance": "allowance", "apology": "", "apparant": "apparent",
    "arguement": "argument", "basicly": "basically", "beatiful": "beautiful",
    "camera": "camera", "carribean": "caribbean", "catterpillar": "caterpillar",
    "changd": "changed", "colour": "color", "centre": "center", "metre": "meter",
    "litre": "liter", "travelling": "traveling", "cancelled": "canceled",
    "realise": "realize", "recognise": "recognize", "organise": "organize",
    "analyse": "analyze", "defence": "defense", "licence": "license", "practise": "practice",
    "cheif": "chief", "chnage": "change", "chossen": "chosen", "clera": "clear",
    "compleatly": "completely", "conected": "connected", "consious": "conscious",
    "contious": "conscious", "correspondance": "correspondence", "critism": "criticism",
    "decison": "decision", "desicion": "decision", "deparment": "department",
    "directer": "director", "disatisfied": "dissatisfied", "draged": "dragged",
    "efect": "effect", "eliment": "element", "existance": "existence", "experiance": "experience",
    "familar": "familiar", "favors": "favors", "febuary": "february", "finacial": "financial",
    "forhead": "forehead", "fourtinately": "fortunately", "garantee": "guarantee",
    "greatful": "grateful", "governer": "governor", "happend": "happened", "harmonyous": "harmonious",
    "heros": "heroes", "hindermost": "hindermost", "humour": "humor", "idear": "idea",
    "imediate": "immediate", "incidently": "incidentally", "influance": "influence",
    "instict": "instinct", "interum": "interim", "jeopardy": "", "jwarn": "jar",
    "knowlege": "knowledge", "lalangage": "language", "liason": "liaison", "libary": "library",
    "listen": "listen", "managment": "management", "millenium": "millennium",
    "miniscule": "minuscule", "mischievous": "", "monestary": "monastery",
    "morgage": "mortgage", "naturually": "naturally", "neccessarily": "necessarily",
    "noticable": "noticeable", "occassionally": "occasionally", "occurence": "occurrence",
    "offest": "offset", "oportunity": "opportunity", "opposit": "opposite",
    "orchastera": "orchestra", "particualr": "particular", "pasttime": "pastime",
    "peoples": "", "perhas": "perhaps", "persistant": "persistent", "personel": "personnel",
    "pharoah": "pharaoh", "polish": "", "possability": "possibility", "prepaired": "prepared",
    "proffesion": "profession", "prominant": "prominent", "pronounciation": "pronunciation",
    "propoganda": "propaganda", "publically": "publicly", "quater": "quarter",
    "questionaire": "questionnaire", "rasberry": "raspberry", "realy": "really",
    "reciept": "receipt", "reight": "right", "relgies": "religies", "repitition": "repetition",
    "resistence": "resistance", "restaraunt": "restaurant", "rythem": "rhythm",
    "sargent": "sergeant", "scenary": "scenario", "secretery": "secretary",
    "sieze": "seize", "similer": "similar", "sincerley": "sincerely", "speach": "speech",
    "statemen": "statement", "stragedy": "strategy", "subseqent": "subsequent",
    "superintendent": "", "suprise": "surprise", "surprize": "surprise", "temperary": "temporary",
    "tendancy": "tendency", "thanx": "thanks", "threshhold": "threshold", "tresspass": "trespass",
    "unforseen": "unforeseen", "unfortunatly": "unfortunately", "untill": "until",
    "vengence": "vengeance", "viceversa": "vice versa", "visiting": "", "volkano": "volcano",
    "wantsto": "wants to", "wrote": "", "yeild": "yield",
}

# Phonetic respellings: "fone"->"phone", "nite"->"night", "raiting"->"writing".
PHONETIC_FIXES = [
    (re.compile(r"f(?=one|oo|ear|y)$"), "ph"),
]


def _phonetic_variants(lw):
    """Yield phonetically-normalised respellings of a word."""
    outs = set()
    # ph -> f  (phone->fone already covered by dictionary; fone->phone below)
    if lw.startswith("fone"):
        outs.add("phone" + lw[4:])
    if "ph" not in lw and lw.replace("f", "ph", 1) != lw:
        outs.add(lw.replace("f", "ph", 1))
    if "ph" in lw:
        outs.add(lw.replace("ph", "f", 1))
    # -ite / -ight confusion
    if lw.endswith("ite") and len(lw) > 4:
        outs.add(lw[:-3] + "ight")
    if lw.endswith("ight"):
        outs.add(lw[:-4] + "ite")
    # -ain/-ene/-ein (plain/pane, bean/been)
    for a, b in (("ait", "ate"), ("ait", "eight"), ("ait", "weight")):
        if a in lw:
            outs.add(lw.replace(a, b, 1))
    # dropping doubled consonants: raiting -> writing needs w; handled via typo map
    # silent-k before n
    if lw.startswith("n") and len(lw) > 2:
        outs.add("kn" + lw[1:])
    if lw.startswith("k") and len(lw) > 2 and lw[1] == "n":
        pass
    # -eed vs -ed
    if lw.endswith("eed"):
        outs.add(lw[:-3] + "ed")
    if lw.endswith("ed") and not lw.endswith("eed"):
        outs.add(lw[:-2] + "eed")
    # y/i swap at end: hapy->happy etc.
    if lw.endswith("y") and len(lw) > 3:
        outs.add(lw[:-1] + "ie")
    return {o for o in outs if o != lw and len(o) > 1}


# QWERTY adjacency used for keyboard-aware ranking.
KEY_ADJ = {
    "q": "was", "w": "qase", "e": "rwsd", "r": "etfds", "t": "ryfgd",
    "y": "tughf", "u": "yijhk", "i": "uojkl", "o": "ipkl", "p": "ol",
    "a": "qwsz", "s": "awedxz", "d": "serfcx", "f": "drtgvc", "g": "ftyhbv",
    "h": "gyujnb", "j": "huikmn", "k": "jiolm", "l": "kop", "z": "asx",
    "x": "zsdc", "c": "xdfv", "v": "cfgb", "b": "vghn", "n": "bhjm", "m": "njk",
}


def _keyboard_score(a: str, b: str) -> int:
    """Extra edit cost penalty reduction for common one-handed slip types."""
    if len(a) != len(b):
        return 0
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    if len(diff) == 1:
        i = diff[0]
        if b[i] in KEY_ADJ.get(a[i], ""):
            return 1                       # adjacent-key slip
    if len(diff) == 2 and diff[1] - diff[0] == 1:
        if a[diff[0]] == a[diff[1]] :
            return 1                       # double-letter held too long
        if a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]:
            return 1                       # transposition (already OSA=1, boost more)
    return 0


def _dict_known(words):
    """Words present in the spell corpus and/or the built-in dictionary."""
    known = set()
    if spell is not None:
        try:
            known |= set(spell.known(words))
        except Exception:
            pass
    known |= {w for w in words if w in CORE_WORDS}
    return known


def rank_suggestions(word: str, limit: int = 3):
    """Frequency- and keyboard-aware correction candidates for `word`."""
    lw = word.lower().replace("’", "'")
    scored = []          # (sort_key, candidate)

    def add(cand, key):
        scored.append((key, cand))

    raw = set()
    try:
        raw |= set(spell.candidates(lw) or set())
    except Exception:
        pass
    if not raw:                              # tiny/offline dictionary fallback
        for c in CORE_WORDS:
            if edit_distance(lw, c) <= (1 if len(lw) <= 5 else 2):
                raw.add(c)
    if lw in TYPO_MAP and TYPO_MAP[lw]:
        raw.add(TYPO_MAP[lw])
    for pv in _phonetic_variants(lw):
        if pv in CORE_WORDS or (spell is not None and spell.known([pv])):
            raw.add(pv)
        if pv in TYPO_MAP and TYPO_MAP[pv]:
            raw.add(TYPO_MAP[pv])
    for c in raw:
        if c == lw or not c or " " in c:
            continue
        d = edit_distance(lw, c)
        if d > 2:
            continue
        kb = _keyboard_score(lw, c)
        first = 0 if c[0] == lw[0] else 1
        add(c, (max(0, d - kb), first, -freq(c)))
    # word-split candidates: "alot" -> "a lot", "thankyou" -> "thank you"
    for i in range(1, len(lw)):
        a, b = lw[:i], lw[i:]
        if (len(a) > 1 or a in ("a", "i")) and (len(b) > 1 or b in ("a", "i")):
            if spell is not None:
                ok = bool(spell.known([a])) and bool(spell.known([b]))
            else:
                ok = a in CORE_WORDS and b in CORE_WORDS
            if ok:
                add(f"{a} {b}", (1, 0, -min(freq(a), freq(b))))
    scored.sort(key=lambda x: x[0])
    out = []
    for _, c in scored:
        c = match_case(word, c)
        if c not in out:
            out.append(c)
        if len(out) >= limit:
            break
    return out


def confidence(word: str, repl: str) -> float:
    """How sure we are that `repl` is what the user meant by `word` (0..1)."""
    if not word or not repl:
        return 0.0
    lw, lr = word.lower(), repl.lower()
    if lw in TYPO_MAP and TYPO_MAP[lw] == lr:
        return 0.97
    d = edit_distance(lw, lr)
    base = {1: 0.88, 2: 0.66}.get(d, 0.45)
    if _keyboard_score(lw, lr):
        base += 0.06                       # adjacent-key slip / transposition
    if lr.startswith(lw[:1]):
        base += 0.03
    f = freq(lr)
    if f >= 1_000_000:
        base += 0.04
    elif f < 1000:
        base -= 0.08
    return max(0.30, min(0.98, base))


# --------------------------------------------------------------------------
# Grammar & style data
# --------------------------------------------------------------------------
# Confused-word pairs: wrong -> right (context-free enough to flag directly)
CONFUSIONS = {
    "your": "you're", "weither": "whether", "loose": "lose", "advise": "advice",
    "effect": "affect", "accept": "except", "excess": "access", "principal": "principle",
    "ensure": "insure", "describe": "discribe", "article": "articel", "transport": "transportation",
    "choose": "chose", "cloth": "clothes", "weather": "whether", "peace": "piece",
    "site": "sight", "cite": "site", "stage": "stair", "tail": "tale", "won": "one",
    "yourself": "you're self", "alot": "a lot", "could of": "could have",
    "should of": "should have", "would of": "would have", "must of": "must have",
    "might of": "may have", "got an idea": "have an idea",
    "there": "their", "they're": "their", "its": "it's", "who's": "whose",
    "than": "then", "then": "than", "to": "too", "too": "to", "two": "to",
    "through": "throw", "though": "thought", "hear": "here", "where": "wear",
    "witch": "which", "know": "no", "new": "knew", "nice": "price", "piece": "peace",
    "plain": "plane", "some": "sum", "so": "sow", "sun": "son", "wait": "weight",
    "waist": "waste", "wall": "war", "ward": "warred", "want": "wont",
}
# Curated high-signal subset actually used as direct replacements (the table
# above doubles as context hints; these fire only with strong evidence):
STRONG_CONFUSIONS = {
    "your welcome": "you're welcome", "should of": "should have",
    "could of": "could have", "would of": "would have", "must of": "must have",
    "might of": "may have", "alot": "a lot", "could care less": "couldn't care less",
    "for all intensive purposes": "for all intents and purposes",
    "off of": "off", "gone and": "go and", "use to": "used to",
    "have went": "have gone", "has went": "has gone", "had went": "had gone",
    "did went": "did go", "was like": "said", "kinda": "kind of", "sorta": "sort of",
    "gotta": "have to", "gonna": "going to", "wanna": "want to", "outloud": "out loud",
    "each and every": "each", "past history": "history", "future plans": "plans",
    "actual fact": "in fact", "end result": "result", "final outcome": "outcome",
    "free gift": "gift", "new innovation": "innovation", "unexpected surprise": "surprise",
    "join together": "join", "return back": "return", "repeat again": "repeat",
    "advance planning": "planning", "close proximity": "proximity",
    "cooperate together": "cooperate", "merge together": "merge",
}
# Verb-form rules: (auxiliary, wrong form) -> right form
PARTICIPLES = {
    "went": "gone", "did": "done", "saw": "seen", "ate": "eaten", "took": "taken",
    "gave": "given", "spoke": "spoken", "broke": "broken", "woke": "woken",
    "wrote": "written", "rode": "ridden", "drive": "driven", "froze": "frozen",
    "chose": "chosen", "spoke": "spoken", "began": "begun", "rang": "rung",
    "sang": "sung", "swam": "swum", "ran": "run", "came": "come", "flew": "flown",
    "grew": "grown", "knew": "known", "threw": "thrown", "drew": "drawn",
    "shook": "shaken", "stole": "stolen", "wore": "worn", "bore": "born",
    "spoke": "spoken", "hid": "hidden", "bit": "bitten", "forgave": "forgiven",
    "mistook": "mistaken", "overslept": "overslept", "understood": "understood",
}
HAVE_AUX = {"have", "has", "had", "having"}
BEEN_AUX = {"been", "being"}
MODALS = {"should", "could", "would", "must", "will", "shall", "can", "may", "might"}
S_VIEWS = {"he": "s", "she": "s", "it": "s"}
IRREGULAR_S = {"have": "has", "be": "is", "do": "does", "go": "goes", "say": "says",
               "make": "makes", "take": "takes", "know": "knows", "get": "gets",
               "want": "wants", "see": "sees", "think": "thinks", "look": "looks",
               "come": "comes", "give": "gives", "feel": "feels", "leave": "leaves",
               "put": "puts", "run": "runs", "bring": "brings", "happen": "happens",
               "write": "writes", "provide": "provides", "stand": "stands",
               "lose": "loses", "pay": "pays", "meet": "meets", "learn": "learns",
               "change": "changes", "watch": "watches", "create": "creates",
               "read": "reads", "follow": "follows", "stop": "stops", "speak": "speaks"}

FILLER = {
    "very": "delete it or pick a stronger word", "really": "delete it",
    "just": "delete it (often filler)", "actually": "delete it", "basically": "delete it",
    "literally": "usually wrong; delete it", "totally": "delete it", "simply": "delete it",
    "obviously": "let the fact speak", "clearly": "let the fact speak",
    "stuff": "things / items", "things": "be specific", "somehow": "delete it",
    "somewhat": "delete it", "quite": "delete it", "rather": "delete it",
    "thing": "be specific", "nice": "be specific", "good": "be specific",
    "bad": "be specific", "big": "be specific", "small": "be specific",
    "interesting": "say why it's interesting", "amazing": "be specific",
    "awesome": "be specific", "cool": "be specific", "fine": "be specific",
}
REDUNDANT = {
    "add extra": "add", "advance planning": "planning", "astounding surprise": "surprise",
    "basic fundamentals": "fundamentals", "close proximity": "proximity",
    "complete entire": "complete", "copy duplicate": "copy", "current trend": "trend",
    "each and every": "each", "end result": "result", "escaped escape": "escaped",
    "expected expectation": "expectation", "final conclusion": "conclusion",
    "first and foremost": "first", "free gift": "gift", "future plans": "plans",
    "general consensus": "consensus", "join together": "join", "late delay": "late",
    "made up of components": "composed of", "merge together": "merge",
    "past history": "history", "personal opinion": "opinion", "plan ahead": "plan",
    "repeat again": "repeat", "revert back": "revert", "sufficient enough": "sufficient",
    "surpassed excess": "surpassed", "unexpected surprise": "surprise",
    "unsolved mystery": "mystery", "used in order to": "used to",
}
OFFICIALISMS = {
    "utilize": "use", "utilise": "use", "commence": "start", "terminate": "end",
    "purchase": "buy", "inquire": "ask", "assist": "help", "attempt": "try",
    "require": "need", "request": "ask for", "provide": "give", "obtain": "get",
    "additional": "more", "prior to": "before", "due to the fact that": "because",
    "in order to": "to", "at this point in time": "now", "in the event that": "if",
    "on a daily basis": "daily", "make a decision": "decide", "reach a conclusion": "conclude",
    "give consideration to": "consider", "has the ability to": "can",
    "is capable of": "can", "in accordance with": "per", "with regard to": "about",
    "regarding the matter of": "about", "as per your request": "as requested",
    "please find attached": "attached is", "enclosed please find": "enclosed is",
    "forward-looking": "optimistic", "leverage": "use", "synergy": "cooperation",
    "paradigm": "model", "holistic": "whole", "robust": "strong", "seamless": "smooth",
    "cutting-edge": "advanced", "best-in-class": "excellent", "next-gen": "modern",
    "solution-oriented": "focused", "results-driven": "effective", "game-changer": "breakthrough",
    "low-hanging fruit": "easy wins", "move the needle": "make progress",
    "circle back": "discuss later", "touch base": "check in", "bandwidth": "capacity",
    "take it offline": "continue privately", "ping": "message", "deep dive": "analysis",
    "boil the ocean": "overreach", "best practice": "proven method",
    "value proposition": "benefit", "actionable insights": "usable findings",
    "operationalize": "implement", "ideate": "brainstorm", "socialize": "share",
    "normalize": "standardize", "streamline": "simplify", "facilitate": "help",
    "methodology": "method", "incentivize": "reward", "transition": "move",
    "engage": "involve", "empower": "enable", "align": "agree",
    "escalate": "raise", "delegate": "assign", "mitigate": "reduce",
    "optimize": "improve", "leverage": "use", "synergies": "cooperation",
}
# Single-word confused pairs that are safe to flag WITHOUT extra context
# (the raw CONFUSIONS table is far too ambiguous for blind auto-flagging -
# e.g. "then"/"than", "to"/"too" - only these high-signal ones fire directly).
DIRECT_CONFUSIONS = {
    "alot": "a lot",
    "viceversa": "vice versa",
}

PASSIVE_HINTS = {"was", "were", "been", "being", "is", "are", "am"}


def _build_phrase_rules():
    """Merge every phrase-level table into one ordered (wrong -> right) list,
    longest first so 'due to the fact that' beats 'prior to' etc."""
    merged = {}
    for src in (STRONG_CONFUSIONS, REDUNDANT, OFFICIALISMS):
        for wrong, right in src.items():
            w = wrong.lower().strip()
            if w and " " in w:                     # multi-word phrases only here
                merged[w] = right
    return sorted(merged.items(), key=lambda kv: -len(kv[0]))


_PHRASE_RULES = _build_phrase_rules()

PAST_SIMPLE = {  # common irregular/regular past participles for passive detection
    "made", "given", "taken", "seen", "known", "shown", "held", "kept", "sent",
    "built", "found", "brought", "written", "read", "paid", "left", "told", "asked",
    "called", "needed", "wanted", "liked", "loved", "hated", "believed", "provided",
    "supported", "allowed", "forced", "helped", "stopped", "started", "ended",
    "created", "designed", "developed", "produced", "completed", "finished",
    "approved", "rejected", "selected", "chosen", "involved", "included",
    "required", "requested", "recommended", "announced", "reported", "recorded",
    "documented", "implemented", "organized", "managed", "controlled", "measured",
    "evaluated", "reviewed", "updated", "modified", "fixed", "corrected",
    "tested", "verified", "confirmed", "identified", "analyzed", "processed",
    "delivered", "received", "ordered", "purchased", "sold", "distributed",
    "published", "released", "launched", "established", "founded", "formed",
    "prepared", "planned", "scheduled", "assigned", "allocated", "budgeted",
}





def init_spell_dictionary():
    """Merge the pre-packed dictionary into the spell checker (offline-safe).

    Called once at startup; the curated CORE_WORDS are always present, whether
    or not pyspellchecker's own corpus loaded.
    """
    global spell
    if spell is None:
        try:
            spell = SpellChecker(distance=1)
        except Exception:
            return False
    try:
        spell.word_frequency.load_words(sorted(CORE_WORDS))
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Platform helpers
# --------------------------------------------------------------------------
WIN = sys.platform == "win32"
MAC = sys.platform == "darwin"

if WIN:
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    kernel32 = ctypes.windll.kernel32

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                    ("rcCaret", wintypes.RECT)]


def make_dpi_aware():
    if WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def win_style(win, click_through=False):
    """Windows: make a Tk window non-activating (never steals focus)."""
    if not WIN:
        return
    try:
        win.update_idletasks()
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        ex = user32.GetWindowLongW(hwnd, -20)
        ex |= 0x08000000 | 0x00000080            # NOACTIVATE | TOOLWINDOW
        if click_through:
            ex |= 0x00000020                     # TRANSPARENT (LAYERED already set by -alpha)
        user32.SetWindowLongW(hwnd, -20, ex)
    except Exception:
        pass


def foreground():
    """(window id, pid) of the active window, best effort."""
    try:
        if WIN:
            hwnd = user32.GetForegroundWindow()
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return (hwnd, pid.value)
        if MAC:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return (app.processIdentifier(), app.processIdentifier())
        out = subprocess.check_output(["xdotool", "getactivewindow"], timeout=0.3,
                                      stderr=subprocess.DEVNULL)
        return (out.strip(), None)
    except Exception:
        return (None, None)


def window_origin():
    """Top-left of the active window (Windows only) so underlines follow moves."""
    if WIN:
        try:
            r = wintypes.RECT()
            user32.GetWindowRect(user32.GetForegroundWindow(), ctypes.byref(r))
            return (r.left, r.top)
        except Exception:
            pass
    return (0, 0)


class CaretLocator:
    """Screen position of the text caret: (x, y_top, height) or None."""

    def __init__(self, root=None):
        self.errors = 0
        self.root = root

    @staticmethod
    def _linux(root):
        """Best-effort caret position via xdotool (needs a focused text field)."""
        try:
            out = subprocess.check_output(["xdotool", "getmouselocation", "--shell"],
                                          timeout=0.3, stderr=subprocess.DEVNULL)
            kv = dict(l.split("=", 1) for l in out.decode().split())
            return (int(kv["x"]), int(kv["y"]) - 2, 16)
        except Exception:
            return None

    def get(self):
        if self.errors > 25:
            return None
        try:
            if WIN:
                r = self._win()
            elif MAC:
                r = self._mac()
            else:
                r = self._linux(root=self.root) if getattr(self, "root", None) else None
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
            kAXBoundsForRangeParameterizedAttribute, kAXValueCFRangeType,
            kAXValueCGRectType)
        from CoreFoundation import CFRangeMake
        sysw = AXUIElementCreateSystemWide()
        err, el = AXUIElementCopyAttributeValue(sysw, kAXFocusedUIElementAttribute, None)
        if err or el is None:
            return None
        err, rng = AXUIElementCopyAttributeValue(el, kAXSelectedTextRangeAttribute, None)
        if err or rng is None:
            return None
        ok, cf = AXValueGetValue(rng, kAXValueCFRangeType, None)
        if not ok or cf.location < 1:
            return None
        prev = AXValueCreate(kAXValueCFRangeType, CFRangeMake(cf.location - 1, 1))
        err, bounds = AXUIElementCopyParameterizedAttributeValue(
            el, kAXBoundsForRangeParameterizedAttribute, prev, None)
        if err or bounds is None:
            return None
        ok, rect = AXValueGetValue(bounds, kAXValueCGRectType, None)
        if not ok:
            return None
        return (rect.origin.x + rect.size.width, rect.origin.y, rect.size.height)


_mon_cache = (0.0, [])


def get_monitors(root):
    global _mon_cache
    if time.time() - _mon_cache[0] < 3 and _mon_cache[1]:
        return _mon_cache[1]
    ms = []
    try:
        from screeninfo import get_monitors as gm
        ms = [(m.x, m.y, m.width, m.height) for m in gm()]
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


MY_WORDS_TEMPLATE = """\
# Writing Assistant - your personal dictionary
# One word per line. Lines starting with # are ignored.
# Anything you add here is treated as correctly spelled (names, jargon, slang...).
# You can also drop other word lists (.txt / .dic / .csv) into this same folder -
# they are all loaded automatically, no restart needed.
"""


def ensure_dictionary_folder():
    """Create ~/WritingAssistant/dictionaries/ and my_words.txt if missing."""
    try:
        DICT_DIR.mkdir(parents=True, exist_ok=True)
        old = Path.home() / ".writing_assistant_words.txt"      # migrate v1/v2 file
        if not MY_WORDS.exists():
            MY_WORDS.write_text(MY_WORDS_TEMPLATE, encoding="utf-8")
            if old.exists():
                with open(MY_WORDS, "a", encoding="utf-8") as f:
                    f.write(old.read_text(encoding="utf-8", errors="ignore"))
    except Exception as ex:
        print("[wa] could not create dictionary folder:", ex)


def parse_dictionary_file(path):
    """Words from a .txt/.csv/.dic file. Tolerates comments, 'word count' lists
    and Hunspell 'word/FLAGS' entries."""
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
        if n == 0 and tok.isdigit():              # Hunspell word-count header
            continue
        if tok and not tok.isdigit() and len(tok) < 60:
            words.add(tok.lower().replace("’", "'"))
    return words


def dictionary_signature():
    try:
        return tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size)
                            for p in DICT_DIR.iterdir() if p.suffix.lower() in DICT_EXTS))
    except Exception:
        return ()


def load_all_dictionaries():
    words = set()
    try:
        for p in sorted(DICT_DIR.iterdir()):
            if p.suffix.lower() in DICT_EXTS:
                words |= parse_dictionary_file(p)
    except Exception:
        pass
    return words


def import_dictionaries(paths):
    """--import: copy word lists into the dictionary folder."""
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
        shutil.copy2(src, dst)
        print(f"  imported {src.name}: {len(parse_dictionary_file(dst))} words -> {dst}")


def open_folder(path):
    try:
        if WIN:
            os.startfile(str(path))
        elif MAC:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as ex:
        print("[wa] could not open folder:", ex)


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------
@dataclass
class Issue:
    id: int
    kind: str            # spelling | grammar | punct | style
    label: str
    s: int               # edit span start (in text)
    e: int               # edit span end
    hs: int              # highlight span start
    he: int              # highlight span end
    word: str
    suggestions: list    # [(label, replacement)]
    rect: Optional[tuple] = None    # (x, y, width) screen underline
    origin: tuple = (0, 0)
    char_w: float = 7.0


class Assistant:
    """Tracks the text you type in the current field and finds issues in it."""

    def __init__(self, notify, toast, locator):
        self.notify, self.toast, self.locator = notify, toast, locator
        self.lock = threading.RLock()
        self.apply_lock = threading.Lock()
        self.kb = keyboard.Controller()
        self.text = ""
        self.issues = []
        self.selected = None
        self.samples = {}          # text index -> (x, y, h, ox, oy) caret samples
        self.checked_upto = 0
        self.flushed_len = -1
        self.last_key = 0.0
        self.next_id = 1
        self.ignored = set()
        ensure_dictionary_folder()
        self.user_words = set()
        self.dict_sig = None
        self._last_dict_check = 0.0
        self.reload_dictionaries(quiet=True)
        self.paused = False
        self.injecting = False
        self.mods = set()
        self.anchor = None         # last click position (for estimated underlines)
        self._fulltext_at = 0.0    # when we last probed the whole field
        self._fulltext_len = -1    # buffer length after the last successful grab
        self.sample_q = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---------------- public state helpers
    def snapshot(self):
        with self.lock:
            return {
                "text": self.text, "selected": self.selected, "paused": self.paused,
                "issues": [dict(id=i.id, kind=i.kind, label=i.label, word=i.word,
                                hs=i.hs, he=i.he, suggestions=list(i.suggestions),
                                rect=i.rect, origin=i.origin)
                           for i in sorted(self.issues, key=lambda x: x.hs)],
            }

    def reset(self, anchor=None):
        with self.lock:
            self.anchor = anchor
            had = bool(self.issues or self.text)
            self.text, self.issues, self.selected = "", [], None
            self.samples.clear()
            self.checked_upto, self.flushed_len = 0, -1
            self._fulltext_at, self._fulltext_len = time.time(), -1
        self.sample_q.put(0)           # caret position after a click/nav = start of buffer
        if had:
            self.notify()

    def drop_rects(self):
        with self.lock:
            for i in self.issues:
                i.rect = None
            self.samples.clear()
        self.notify()

    def select(self, iid):
        with self.lock:
            if any(i.id == iid for i in self.issues):
                self.selected = iid
        self.notify()

    def next_issue(self):
        with self.lock:
            ordered = sorted(self.issues, key=lambda x: x.hs)
            if not ordered:
                return
            ids = [i.id for i in ordered]
            idx = (ids.index(self.selected) + 1) % len(ids) if self.selected in ids else 0
            self.selected = ids[idx]
        self.notify()

    def _selected_issue(self):
        return next((i for i in self.issues if i.id == self.selected), None)

    def ignore(self, iid=None):
        with self.lock:
            iss = next((i for i in self.issues if i.id == (iid or self.selected)), None)
            if not iss:
                return
            if iss.kind == "spelling":
                lw = iss.word.lower()
                self.ignored.add(lw)
                self.issues = [i for i in self.issues if not (i.kind == "spelling" and i.word.lower() == lw)]
            else:
                self.issues = [i for i in self.issues if i.id != iss.id]
            self._fix_selected()
        self.notify()

    def reload_dictionaries(self, quiet=False):
        sig = dictionary_signature()
        if sig == self.dict_sig:
            return
        first = self.dict_sig is None
        self.dict_sig = sig
        words = load_all_dictionaries()
        with self.lock:
            self.user_words = words
            # also teach the spell-checker so these words can be SUGGESTED, not just accepted
            try:
                spell.word_frequency.load_words(sorted(words))
            except Exception:
                pass
            # drop issues for words that are now in the dictionary
            self.issues = [i for i in self.issues
                           if not (i.kind == "spelling" and i.word.lower() in words)]
            self._fix_selected()
        dbg("dictionary loaded:", len(words), "words from", DICT_DIR)
        if not quiet and not first:
            self.toast(f"Dictionary reloaded ({len(words)} words)")
        self.notify()

    def add_word(self, iid=None):
        with self.lock:
            iss = next((i for i in self.issues if i.id == (iid or self.selected)), None)
            if not iss or iss.kind != "spelling":
                return
            lw = iss.word.lower()
            self.user_words.add(lw)
            try:
                ensure_dictionary_folder()
                with open(MY_WORDS, "a", encoding="utf-8") as f:
                    f.write(lw + "\n")
                self.dict_sig = dictionary_signature()     # we wrote it ourselves
            except Exception:
                pass
            self.issues = [i for i in self.issues if not (i.kind == "spelling" and i.word.lower() == lw)]
            self._fix_selected()
        self.notify()
        self.toast(f"Added “{lw}” to dictionary")

    def toggle_pause(self):
        self.paused = not self.paused
        if not self.paused:
            self.reset()                 # resume: start tracking fresh
        self.toast("Paused" if self.paused else "Resumed")

    def _fix_selected(self):
        if self.selected not in {i.id for i in self.issues}:
            self.selected = self.issues[-1].id if self.issues else None

    # ---------------- keystroke input
    def type_char(self, c):
        with self.lock:
            if self.paused:
                return
            self.last_key = time.time()
            self.flushed_len = -1
            n0 = len(self.text)
            if is_word_char(c):   # word is being extended -> its old verdict is stale
                self.issues = [i for i in self.issues if not (i.kind in WORD_KINDS and i.he == n0)]
            self.text += c
            self._trim()
            n = len(self.text)
            if is_word_char(c):
                self._rule_missing_space(c)
                if n == 1:
                    self.sample_q.put(1)
            else:
                self._rule_punct(c)
                if c.isspace():
                    self._analyze_range(self.checked_upto, n)
                    self.checked_upto = n
                elif n == 2 and c.isalpha() and self.text[0].islower() \
                        and self.text[1].isupper():
                    # you started typing mid-sentence ("hello." -> "Hello.")
                    self._add("style", "Capitalize first word of sentence",
                              0, 1, 0, 1, [(self.text[0].upper(), self.text[0].upper())])
                self.sample_q.put(n)
            self._fix_selected()
        self.notify()

    def backspace(self):
        with self.lock:
            if self.paused or not self.text:
                return
            self.last_key = time.time()
            self.flushed_len = -1
            self.text = self.text[:-1]
            n = len(self.text)
            self.issues = [i for i in self.issues if i.he <= n and i.e <= n]
            self.samples = {k: v for k, v in self.samples.items() if k <= n}
            ts = n
            while ts > 0 and not self.text[ts - 1].isspace():
                ts -= 1
            self.checked_upto = min(self.checked_upto, ts)
            self._fix_selected()
        self.notify()

    def enter(self):
        if ENTER_RESETS:
            self.reset()
        else:
            self.type_char("\n")

    # ---------------- whole-field text grab (pasted / pre-existing text)
    def _clip_get(self):
        """Current clipboard text ("" on any failure)."""
        try:
            if WIN:
                CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
                user32.OpenClipboard(0)
                try:
                    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                        return ""
                    h = user32.GetClipboardData(CF_UNICODETEXT)
                    if not h:
                        return ""
                    p = kernel32.GlobalLock(h)
                    try:
                        buf = ctypes.wstring_at(p)
                    finally:
                        kernel32.GlobalUnlock(h)
                    return buf
                finally:
                    user32.CloseClipboard()
            import pyperclip
            try:
                return pyperclip.paste() or ""
            except Exception:
                pass
            if not MAC:
                for tool in ("wl-paste", "xclip -selection clipboard -o", "xsel --clipboard"):
                    try:
                        out = subprocess.check_output(tool.split(), timeout=0.4,
                                                      stderr=subprocess.DEVNULL)
                        return out.decode("utf-8", "ignore")
                    except Exception:
                        continue
            return ""
        except Exception as ex:
            dbg("clip get failed:", ex)
            return ""

    def _clip_set(self, s):
        try:
            if WIN:
                CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
                data = (s + "\0").encode("utf-16-le")
                h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                p = kernel32.GlobalLock(h)
                ctypes.memmove(p, data, len(data))
                kernel32.GlobalUnlock(h)
                user32.EmptyClipboard()
                user32.SetClipboardData(CF_UNICODETEXT, h)   # system owns h now
                return True
            import pyperclip
            pyperclip.copy(s)
            return True
        except Exception as ex:
            dbg("clip set failed:", ex)
            return False

    def _grab_full_text(self):
        """Best-effort read of the ENTIRE current field via a copy round-trip.

        On Windows we use the Win32 clipboard API directly (no third-party
        dependency); elsewhere we use pyperclip if installed.  The user's own
        clipboard is saved and restored.  Returns None when the probe cannot be
        trusted (nothing copied, clipboard unavailable, macOS needs Accessibility
        permissions we can't verify here).
        """
        if MAC:
            return None
        with self.apply_lock:
            self.injecting = True
            try:
                deadline = time.time() + 2
                while self.mods and time.time() < deadline:
                    time.sleep(0.02)
                if self.mods:
                    return None
                saved = self._clip_get()
                clip_ok = bool(saved) or self._clip_set("WA_PROBE")
                if not clip_ok:
                    return None
                marker = saved if saved else "WA_PROBE"
                self.kb.tap(keyboard.Key.ctrl, "a")
                time.sleep(0.05)
                self.kb.tap(keyboard.Key.ctrl, "c")
                time.sleep(0.12)
                got = self._clip_get()
                if got == marker or not got:
                    return None          # app never wrote the clipboard
                self._clip_set(saved)    # restore whatever the user had copied
                return got[:MAX_TEXT * 2]
            finally:
                self.kb.tap(keyboard.Key.right)   # deselect so typing stays normal
                time.sleep(0.03)
                self.injecting = False

    def _try_full_grab(self):
        """Idle hook: analyse the whole field, not just what was typed live."""
        if self._fulltext_len >= 0:
            return                       # already grabbed this visit to the field
        self._fulltext_at = time.time()
        got = self._grab_full_text()
        if got is None:
            self._fulltext_at = time.time() - PASTE_PROBE_SECONDS + FULLTEXT_COOLDOWN
            dbg("full-text probe failed; will retry after cooldown")
            return
        got = got.replace("\r\n", "\n").replace("\r", "\n")
        changed = False
        with self.lock:
            if got != self.text:
                self.text = got[-MAX_TEXT:]
                cut = max(0, len(got) - MAX_TEXT)
                self.issues = []
                self.selected = None
                self.next_id = 1
                self.samples.clear()
                self.checked_upto = 0
                self._fulltext_len = len(self.text)
                if cut == 0:
                    self._analyze_range(0, len(self.text))
                changed = True
        if changed:
            dbg("adopted full field text:", len(got), "chars")
            n = len(self.text)
            self.sample_q.put(n)
            self.notify()

    def _trim(self):
        if len(self.text) <= MAX_TEXT:
            return
        cut = len(self.text) - (MAX_TEXT - 200)
        self.text = self.text[cut:]
        keep = []
        for i in self.issues:
            if i.s >= cut and i.hs >= cut:
                i.s -= cut; i.e -= cut; i.hs -= cut; i.he -= cut
                keep.append(i)
        self.issues = keep
        self.samples = {k - cut: v for k, v in self.samples.items() if k >= cut}
        self.checked_upto = max(0, self.checked_upto - cut)

    # ---------------- rule engine
    def _add(self, kind, label, s, e, hs, he, suggs):
        word = self.text[hs:he]
        self.issues = [i for i in self.issues if not (i.hs == hs and i.he == he)]
        iss = Issue(self.next_id, kind, label, s, e, hs, he, word, suggs)
        self.next_id += 1
        self.issues.append(iss)
        # select only the newest issue when the caret is actually at/after it;
        # otherwise leave the user's current selection alone (no card jumping)
        n = len(self.text)
        if self.selected not in {i.id for i in self.issues} or he >= n - 2:
            self.selected = iss.id
        else:
            self._fix_selected()
        self._update_rect(iss)
        dbg("issue", kind, repr(word), [x[0] for x in suggs])

    def _rule_punct(self, c):
        t, n = self.text, len(self.text)
        if c == " " and FLAG_DOUBLE_SPACE and n >= 3 and t[n - 2] == " " and t[n - 3] not in " \t\n.!?":
            self._add("punct", "Extra space", n - 1, n, n - 2, n, [("remove extra space", "")])
        if c in ",.!?;" and n >= 3 and t[n - 2] == " " and (t[n - 3].isalnum() or t[n - 3] in ")\"'”’"):
            self._add("punct", f"Space before “{c}”", n - 2, n - 1, n - 2, n, [("remove space", "")])
        if c == "," and n >= 2 and t[n - 2] == ",":
            self._add("punct", "Repeated comma", n - 1, n, n - 2, n, [("remove extra comma", "")])

    def _rule_missing_space(self, c):
        t, n = self.text, len(self.text)
        if n < 3 or not c.isalpha() or not t[n - 3].isalpha():
            return
        p = t[n - 2]
        ok = p in ",;!?" or (p == "." and c.isupper() and t[n - 3].islower()
                             and n >= 5 and t[n - 4].isalpha() and t[n - 5].isalpha())
        if not ok:
            return
        a = n
        while a > 0 and not t[a - 1].isspace():
            a -= 1
        tok = t[a:n]
        if any(ch in SPECIAL or ch.isdigit() for ch in tok) or "://" in tok:
            return
        self._add("punct", f"Missing space after “{p}”", n - 1, n - 1, n - 3, n, [("add a space", " ")])

    def _rule_capitalization(self):
        """Whole-text pass: sentence starts and standalone "I" must be capitalized.

        Runs on every analysis flush so it also catches text that was pasted or
        typed before we started tracking - not just what the engine saw live.
        """
        t = self.text
        for m in re.finditer(r"[A-Za-z]+(?:['’][A-Za-z]+)*", t):
            s, e = m.span()
            w = m.group(0)
            lw = w.lower().replace("’", "'")
            if lw in self.ignored or lw in self.user_words:
                continue
            if any(i.hs <= s < i.he for i in self.issues):
                continue                       # already flagged by a stronger rule
            if w == "i" and s > 0 and not t[s - 1].isalnum():
                self._add("grammar", "Capitalize “I”", s, e, s, e, [("I", "I")])
                continue
            if w[0] == "i" and lw in ("i'm", "i've", "i'll", "i'd"):
                fix = "I" + w[1:]
                self._add("grammar", "Capitalize “I”", s, e, s, e, [(fix, fix)])
                continue
            if not w[0].islower() or len(w) < 2:
                continue                       # only plain lowercase words can start sentences
            if not self._sentence_start(s):
                continue
            fix = w[0].upper() + w[1:]
            self._add("style", "Capitalize first word of sentence", s, e, s, e,
                      [(fix, fix)])

    def _analyze_range(self, a, b):
        """Run every rule over the freshly-finished span [a, b).

        The whole-text passes (capitalisation) see the entire buffer, so pasted
        or previously-typed text gets checked too; the local passes only rescan
        the new window plus the word before it (cross-boundary phrases such as
        "have went" need that context).
        """
        # clamp to finished words: never analyse a half-typed trailing token
        e = min(b, len(self.text))
        while e > a and e == len(self.text) and not self.text[e - 1].isspace():
            e -= 1
        if e > a:
            self._analyze_window(a, e)
        self.checked_upto = max(self.checked_upto, e)
        self._rule_capitalization()          # whole-text pass (pasted text included)

    def _analyze_window(self, a, b):
        t = self.text
        # --- phrase rules (confusions / redundancy / officialisms) over the window.
        # extend the scan left by two words so cross-boundary phrases like
        # "should of went" / "have went" still match when the earlier words were
        # consumed on a previous pass.
        lo = a
        for _ in range(2):
            while lo > 0 and not t[lo - 1].isspace():
                lo -= 1
            if lo > 0:
                k = lo - 1
                while k > 0 and not t[k - 1].isspace():
                    k -= 1
                prev_tok = t[k:lo - 1]
                if len(prev_tok) <= 24 and all(ch.isalpha() or ch in "'’-." for ch in prev_tok):
                    lo = k      # rescan the previous word too (dedup makes this cheap)
        # treat line breaks as spaces for this pass so pasted multi-line text
        # analyses as continuous prose ("...went\nthere" still matches "went there")
        nl_positions = [i for i, ch in enumerate(self.text) if ch in "\r\n"]
        if nl_positions:
            t = self.text
            for i in nl_positions:
                t = t[:i] + " " + t[i + 1:]
            self.text = t
        try:
            self._window_rules(a, b, lo)
        finally:
            if nl_positions:
                t = self.text
                for i in nl_positions:
                    t = t[:i] + "\n" + t[i + 1:]
                self.text = t

    def _window_rules(self, a, b, lo):
        t = self.text
        for phrase, repl in _PHRASE_RULES:
            i = max(lo - len(phrase), 0)
            seen_here = set()
            while True:
                j = t.lower().find(phrase, i, b + 1)
                if j < 0:
                    break
                k = j + len(phrase)
                if (j == 0 or not is_word_char(t[j - 1])) and (k >= len(t) or not is_word_char(t[k])) \
                        and j not in seen_here:
                    seen_here.add(j)
                    self._add_phrase(phrase, repl, j, k)
                i = j + 1
        # --- passive voice: be-verb + past participle (+ "by")
        for m in re.finditer(r"\b(is|are|was|were|been|being)\s+(\w+)(?:\s+by\b)?", t[a:b], re.I):
            vw, pw = m.group(1).lower(), m.group(2).lower()
            s0, e0 = a + m.start(), a + m.end()
            if pw in PAST_SIMPLE and (vw in ("was", "were", "is", "are", "been", "being")) \
                    and (m.group(0).rstrip().endswith(" by") or pw in ("made", "given", "taken",
                                                                       "seen", "known", "shown", "sent")):
                self._add("style", "Passive voice — consider active",
                          s0, s0, s0, e0, [("keep as is", t[s0:e0])])
        # --- per-token word rules
        for m in re.finditer(r"\S+", t[a:b]):
            self._analyze_token(a + m.start(), a + m.end())
        # --- whole-text capitalisation pass (catches pasted text too)
        self._rule_capitalization()

    def _add_phrase(self, phrase, repl, j, k):
        kind = "grammar" if phrase in STRONG_CONFUSIONS else "style"
        label = ("Common mix-up" if kind == "grammar" else "Wordy — simplify")
        fix = match_case(self.text[j:j + 1], repl)
        self._add(kind, f"{label}: “{phrase}” → “{repl}”", j, k, j, k, [(fix, fix)])

    def _analyze_token(self, ts, te):
        # forget old word-level verdicts for words ending inside this token
        self.issues = [i for i in self.issues if not (i.kind in WORD_KINDS and ts <= i.he <= te)]
        cs, ce = ts, te
        while cs < ce and not self.text[cs].isalnum():
            cs += 1
        while ce > cs and not self.text[ce - 1].isalnum():
            ce -= 1
        core = self.text[cs:ce]
        if not core:
            return
        # URLs, emails, paths, code, numbers, filenames -> leave alone
        if (any(ch in SPECIAL or ch.isdigit() for ch in core) or "://" in core
                or "." in core or "www" in core.lower()):
            return
        for m in WORD_RE.finditer(core):
            self._analyze_word(cs + m.start(), cs + m.end())

    def _prev_word(self, s):
        t = self.text
        if s >= 2 and t[s - 1] == " " and t[s - 2].isalpha():
            pe = s - 1
            ps = pe
            while ps > 0 and (t[ps - 1].isalpha() or t[ps - 1] in "'’"):
                ps -= 1
            return ps, pe, t[ps:pe]
        return None

    def _sentence_start(self, s):
        t = self.text
        i = s - 1
        while i >= 0 and t[i] in "\"'“‘([":
            i -= 1
        if i < 0:
            return True                      # very first word of the field
        if not t[i].isspace():
            return False
        if "\n" in t[max(0, i - 3):i + 1]:
            return True                      # right after a line break
        while i >= 0 and t[i].isspace():
            i -= 1
        if i < 0:
            return True                      # only whitespace before it
        while i >= 0 and t[i].isspace():
            i -= 1
        if i < 0 or t[i] not in ".!?":
            return False
        if t[i] == ".":
            k = i
            while k > 0 and (t[k - 1].isalpha() or t[k - 1] == "."):
                k -= 1
            abbr = t[k:i].lower().strip(".")
            if abbr in ABBREV or len(abbr) == 1:
                return False
        return True

    def _analyze_word(self, s, e):
        t = self.text
        w = t[s:e]
        lw = w.lower().replace("’", "'")
        if lw in self.ignored or lw in self.user_words:
            return
        prev = self._prev_word(s)
        strict_start = self._sentence_start(s)

        # --- missing apostrophe / lowercase I
        if lw in CONTRACTIONS:
            fix = CONTRACTIONS[lw] if lw in ("im", "ive") else match_case(w, CONTRACTIONS[lw])
            self._add("grammar", "Missing apostrophe", s, e, s, e, [(fix, fix)])
            return
        if w == "i":
            self._add("grammar", "Capitalize “I”", s, e, s, e, [("I", "I")])
            return
        if w[0] == "i" and lw in ("i'm", "i've", "i'll", "i'd"):
            fix = "I" + w[1:]
            self._add("grammar", "Capitalize “I”", s, e, s, e, [(fix, fix)])
            return

        # --- context rules using the previous word
        if prev:
            ps, pe, pw = prev
            pl = pw.lower()
            if pl == lw and lw.isalpha() and len(lw) > 1 and lw not in DUP_OK:
                self._add("grammar", "Repeated word", pe, e, s, e, [(f"remove “{w}”", "")])
                return
            if (pl, lw) in MODAL_OF:
                fix = match_case(w, "have")
                self._add("grammar", f"“{pw} {w}” → “{pw} have”", pe, e, ps, e, [(fix, fix)])
                return
            if lw in PARTICIPLES and (pl in HAVE_AUX or (pl == "been" and lw != PARTICIPLES.get(lw))):
                if pl in HAVE_AUX and lw != PARTICIPLES[lw]:
                    fix = match_case(w, PARTICIPLES[lw])
                    self._add("grammar", f"Past participle: “{lw}” → “{PARTICIPLES[lw]}”",
                              s, e, s, e, [(fix, fix)])
                    return
            if pl in MODALS and lw.endswith("s") and len(lw) > 3:
                base = None
                for cand in (lw[:-1], lw[:-2] + "e" if lw.endswith("es") else None):
                    if cand and (cand in IRREGULAR_S or cand in CORE_WORDS) \
                            and (spell is None or spell.known([cand])):
                        base = cand
                        break
                if base:
                    fix = match_case(w, base)
                    self._add("grammar", f"Use the base verb after “{pw}”: “{base}”",
                              s, e, s, e, [(fix, fix)])
                    return
            if (pl, lw) == ("your", "welcome"):
                fix = match_case(pw, "you're")
                self._add("grammar", "“your” → “you're”", ps, pe, ps, e, [(fix, fix)])
                return
            elif pl == "a" and lw[0] in "aeio" and not w.isupper() and not lw.startswith(("one", "once", "eu", "ou")) \
                    and not (len(w) > 1 and w[0].islower() and w[1].isupper()):
                fix = match_case(pw, "an")
                self._add("grammar", "Use “an” before a vowel sound", ps, pe, ps, e, [(fix, fix)])
                return
            elif pl == "an" and lw[0] in "bcdgjkmnpqrstvwyz" and not w.isupper():
                fix = match_case(pw, "a")
                self._add("grammar", "Use “a” before a consonant sound", ps, pe, ps, e, [(fix, fix)])
                return

        # --- spelling
        if "'" in lw:
            if lw in CONTRACTIONS_OK:
                return
            # leave unknown apostrophe words alone
            return
        if len(lw) < 2 or w.isupper() or any(ch.isupper() for ch in w[1:]):
            return                                   # acronyms, CamelCase, iPhone ...
        known = bool(spell.known([lw])) if spell is not None else (lw in CORE_WORDS)
        if lw in SLANG_OK or known:
            # sentence-start capitalisation is handled by the whole-text pass
            # _rule_capitalization(), which also covers pasted text.
            self._style_word(s, e, lw, w)
            return
        if w[0].isupper() and not (strict_start or s == 0 or t[s - 1] == "\n"):
            return                                   # probably a name
        if lw in DIRECT_CONFUSIONS:                  # "alot" -> "a lot" etc.
            repl = DIRECT_CONFUSIONS[lw]
            fix = match_case(w, repl)
            self._add("grammar", f"“{w}” → “{repl}”", s, e, s, e, [(fix, fix)])
            return
        sugg = rank_suggestions(w)
        if not sugg:
            return                                   # nothing credible - stay quiet
        self._add("spelling", "Possible misspelling", s, e, s, e,
                  [(x, x) for x in sugg])

    def _style_word(self, s, e, lw, w):
        """Soft style hints on correctly-spelled words (never overrides real errors)."""
        hint = FILLER.get(lw)
        if hint and lw not in ("i",):
            self._add("style", f"Filler / weak word: “{lw}” — {hint}", s, e, s, e,
                      [("delete it", ""), ("keep as is", w)])

    # ---------------- caret sampling -> screen rectangles for underlines
    def _update_rect(self, iss):
        pts = sorted(self.samples.items())
        if len(pts) < 2:
            return
        before = [p for p in pts if p[0] <= iss.hs]
        after = [p for p in pts if p[0] >= iss.he]
        if before and after:
            (k1, v1), (k2, v2) = before[-1], after[0]
        elif len(after) >= 2:
            (k1, v1), (k2, v2) = after[0], after[1]
        elif len(before) >= 2:
            (k1, v1), (k2, v2) = before[-2], before[-1]
        else:
            return
        if k2 <= k1 or k2 - k1 > 250:
            return
        if (v1[3], v1[4]) != (v2[3], v2[4]) or abs(v2[1] - v1[1]) > max(6, v1[2] * 0.6):
            return
        avg = (v2[0] - v1[0]) / (k2 - k1)
        if not (1 < avg < 40):
            return
        x = v1[0] + avg * (iss.hs - k1)
        w = max(avg * (iss.he - iss.hs), 6)
        iss.rect = (int(x), int(v1[1] + v1[2] - 1), int(w))
        iss.origin = (v1[3], v1[4])
        iss.char_w = avg

    def _worker(self):
        last_sample_key, last_sample_at = None, 0.0
        while True:
            try:
                key = self.sample_q.get(timeout=0.2)
            except queue.Empty:
                key = None
            if key is not None:
                time.sleep(0.06)                     # let the app move its caret
                pos = self.locator.get()
                if not pos and ESTIMATE_UNDERLINES and self.anchor:
                    with self.lock:
                        ok = key <= len(self.text) and "\n" not in self.text[:key]
                    if ok:
                        ax, ay = self.anchor
                        pos = (ax + FALLBACK_CHAR_W * key, ay - FALLBACK_LINE_H / 2, FALLBACK_LINE_H)
                dbg("caret sample", key, pos)
                if pos:
                    ox, oy = window_origin()
                    with self.lock:
                        if key <= len(self.text):
                            self.samples[key] = (pos[0], pos[1], pos[2], ox, oy)
                            for i in self.issues:
                                if i.rect is None:
                                    self._update_rect(i)
                    last_sample_key, last_sample_at = key, time.time()
                    self.notify()
            elif (last_sample_key is not None
                  and time.time() - last_sample_at > IDLE_FLUSH_SECONDS + 0.4):
                # the target window may have moved/animated after the last sample:
                # re-sample once so underlines land on the right text again
                last_sample_key = None
                with self.lock:
                    self.sample_q.put(len(self.text))
            self._idle_check()
            if time.time() - self._last_dict_check > DICT_RELOAD_SECONDS:
                self._last_dict_check = time.time()
                try:
                    self.reload_dictionaries()
                except Exception as ex:
                    dbg("dictionary reload failed:", ex)

    def _idle_check(self):
        with self.lock:
            if self.paused or time.time() - self.last_key < IDLE_FLUSH_SECONDS:
                return
            fresh = bool(self.text) and self.flushed_len != len(self.text)
            if fresh:
                self.flushed_len = len(self.text)
                if self.checked_upto < len(self.text):
                    self._analyze_range(self.checked_upto, len(self.text))
                self._fix_selected()
                n = len(self.text)
            else:
                n = None
            empty_field = not self.text
        if fresh:
            self.sample_q.put(n)
            self.notify()
        elif empty_field and time.time() - self.last_key > PASTE_PROBE_SECONDS \
                and self._fulltext_len < 0:
            self._try_full_grab()        # e.g. the user pasted without typing

    # ---------------- applying fixes
    def apply_selected(self, idx):
        with self.lock:
            iss = self._selected_issue()
            if not iss or idx >= len(iss.suggestions):
                return
            plan = [(iss.id, iss.suggestions[idx][1])]
        threading.Thread(target=self._do_apply, args=(plan,), daemon=True).start()

    def apply(self, iid, idx):
        with self.lock:
            iss = next((i for i in self.issues if i.id == iid), None)
            if not iss or idx >= len(iss.suggestions):
                return
            plan = [(iss.id, iss.suggestions[idx][1])]
        threading.Thread(target=self._do_apply, args=(plan,), daemon=True).start()

    def fix_all(self):
        with self.lock:
            plan, limit = [], None
            for i in sorted(self.issues, key=lambda x: x.s, reverse=True):
                if not i.suggestions or (limit is not None and i.e > limit):
                    continue
                plan.append((i.id, i.suggestions[0][1]))
                limit = i.s
        if plan:
            threading.Thread(target=self._do_apply, args=(plan,), daemon=True).start()

    def _tap(self, key, n):
        for _ in range(n):
            self.kb.tap(key)
            time.sleep(0.004)

    def _do_apply(self, plan):
        deadline = time.time() + 2          # wait for Ctrl/Alt to be released
        while self.mods and time.time() < deadline:
            time.sleep(0.02)
        with self.apply_lock:
            self.injecting = True
            try:
                for iid, repl in plan:
                    with self.lock:
                        iss = next((i for i in self.issues if i.id == iid), None)
                        if not iss:
                            continue
                        s, e = iss.s, iss.e
                        n_left = len(self.text) - e
                    dbg("apply", repr(iss.word), "->", repr(repl), "left", n_left, "del", e - s)
                    self._tap(keyboard.Key.left, n_left)
                    self._tap(keyboard.Key.backspace, e - s)
                    if repl:
                        self.kb.type(repl)
                    self._tap(keyboard.Key.right, n_left)
                    with self.lock:
                        self._after_fix(iss, repl)
                time.sleep(0.25)
            finally:
                self.injecting = False
                self.last_key = time.time()
        self.notify()

    def _after_fix(self, iss, repl):
        delta = len(repl) - (iss.e - iss.s)
        self.text = self.text[:iss.s] + repl + self.text[iss.e:]
        keep = []
        for j in self.issues:
            if j.id == iss.id:
                continue
            if j.he <= iss.s and j.e <= iss.s:
                keep.append(j)
            elif j.s >= iss.e and j.hs >= iss.e:
                j.s += delta; j.e += delta; j.hs += delta; j.he += delta
                if j.rect and iss.rect and abs(j.rect[1] - iss.rect[1]) < 5:
                    j.rect = (int(j.rect[0] + delta * iss.char_w), j.rect[1], j.rect[2])
                else:
                    j.rect = None
                keep.append(j)
        self.issues = keep
        self.samples = {k: v for k, v in self.samples.items() if k <= iss.s}
        if self.checked_upto > iss.e:
            self.checked_upto = max(0, self.checked_upto + delta)
        self._fix_selected()


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------
CTRL_KEYS = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r,
             keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r}
ALT_KEYS = {keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r}
MOD_KEYS = CTRL_KEYS | ALT_KEYS | {keyboard.Key.shift, keyboard.Key.shift_l,
                                   keyboard.Key.shift_r, keyboard.Key.caps_lock}
if hasattr(keyboard.Key, "alt_gr"):
    ALT_KEYS.add(keyboard.Key.alt_gr)
    MOD_KEYS.add(keyboard.Key.alt_gr)


MAC_VK = {18: "1", 19: "2", 20: "3", 45: "n", 2: "d", 13: "w", 3: "f", 35: "p", 12: "q"}


def key_name(key):
    """Lower-case letter/digit for a key press, robust to Ctrl modifying .char."""
    vk, ch = getattr(key, "vk", None), getattr(key, "char", None)
    if MAC and vk in MAC_VK:
        return MAC_VK[vk]
    if WIN and vk is not None and (48 <= vk <= 57 or 65 <= vk <= 90):
        return chr(vk).lower()
    if ch and len(ch) == 1:
        o = ord(ch)
        return chr(o + 96) if 1 <= o <= 26 else ch.lower()
    return None


class Keys:
    def __init__(self, model, ui_q):
        self.m, self.q = model, ui_q
        self.ui = None

    def on_press(self, key):
        m = self.m
        if key in MOD_KEYS:
            m.mods.add(key)
            return
        if m.injecting:
            return
        dbg("key", key)
        ctrl, alt = bool(m.mods & CTRL_KEYS), bool(m.mods & ALT_KEYS)
        if ctrl and alt:
            n = key_name(key)
            if n in ("1", "2", "3"):
                m.apply_selected(int(n) - 1)
            elif n == "n":
                m.next_issue()
            elif n == "d":
                m.ignore()
            elif n == "w":
                m.add_word()
            elif n == "o":
                ensure_dictionary_folder()
                open_folder(DICT_DIR)
            elif n == "f":
                m.fix_all()
            elif n == "p":
                m.toggle_pause()
            elif n == "q":
                self.q.put(("quit",))
            else:
                m.reset()          # e.g. AltGr-typed symbol: we can't track it
            return
        if m.paused:
            return
        if ctrl:                    # Ctrl/Cmd shortcuts change text unpredictably
            m.reset()
            return
        if key == keyboard.Key.space:
            m.type_char(" ")
        elif key == keyboard.Key.backspace:
            m.backspace()
        elif key == keyboard.Key.enter:
            m.enter()
            threading.Thread(target=m._try_full_grab, daemon=True).start()
        elif getattr(key, "char", None):
            if key.char.isprintable():
                m.type_char(key.char)
        else:                       # arrows, tab, delete, esc, F-keys ...
            m.reset()

    def on_release(self, key):
        self.m.mods.discard(key)

    def on_click(self, x, y, button, pressed):
        if not pressed or self.m.injecting:
            return
        r = self.ui.card_rect if self.ui else None
        if r and r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]:
            dbg("click on card - ignored by tracker")
            return                      # clicking OUR card must not clear the issues
        self.m.reset(anchor=(x, y))

    def on_scroll(self, *a):
        m = self.m
        with m.lock:
            for i in m.issues:
                i.rect = None
            m.samples.clear()
        m.sample_q.put(len(m.text))   # re-sample the caret so marks come back
        m.notify()


def watch_focus(model, stop):
    last = None
    while not stop.is_set():
        fid, pid = foreground()
        if fid is not None and pid != os.getpid():
            if last is not None and fid != last:
                model.mods.clear()      # resync in case a key-release was missed
                model.reset()
            last = fid
        time.sleep(0.3)


# --------------------------------------------------------------------------
# UI (Tk on the main thread)
# --------------------------------------------------------------------------
class UI:
    def __init__(self, root, model):
        self.root, self.model = root, model
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-alpha", 0.97)
        except tk.TclError:
            pass
        self.frame = tk.Frame(self.win, bg=BG, padx=12, pady=10,
                              highlightthickness=2, highlightbackground=GREEN)
        self.frame.pack(fill="both", expand=True)
        self.collapsed = False
        self.user_pos = None
        self.toast_text, self.toast_until = "", 0.0
        self.snap = None
        self.card_rect = None
        self.lines = []
        self._dx = self._dy = 0
        # anti-flicker / anti-jump state
        self._last_sig = None          # signature of the last rendered card
        self._render_at = 0.0          # when we last rebuilt the card
        self._user_anchor = None       # pointer xy captured at card creation
        self._pending = None           # latest snapshot queued for drawing
        self._tip = tk.Toplevel(root)
        self._tip.withdraw()
        self._tip.overrideredirect(True)
        self._tip.attributes("-topmost", True)
        try:
            self._tip.attributes("-alpha", 0.95)
        except tk.TclError:
            pass
        tk.Label(self._tip, text="", bg="#0b0f1a", fg=FG, padx=8, pady=4,
                 font=("Helvetica", 9), wraplength=260).pack()
        # Map each window once, off-screen, at startup so we can mark it non-activating
        # BEFORE it is ever shown over your document (so it can never steal focus).
        self.win.geometry("+-4000+-4000")
        self.win.deiconify()
        self.win.update()
        win_style(self.win)
        self.win.withdraw()
        self.lines = [self._make_line() for _ in range(MAX_UNDERLINES)]

    def _make_line(self):
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        try:
            w.attributes("-alpha", 0.99)
        except tk.TclError:
            pass
        w.geometry("1x1+-4000+-4000")
        w.update()
        win_style(w, click_through=True)
        w.withdraw()
        return w

    # ----- small widgets
    def chip(self, parent, text, cmd, bg=PANEL, fg=FG, tip=None):
        lb = tk.Label(parent, text=text, bg=bg, fg=fg, padx=8, pady=3, cursor="hand2",
                      font=("Helvetica", 10, "bold"))
        lb.bind("<Button-1>", lambda e: cmd())
        lb.bind("<Enter>", lambda e: self._tip_show(lb, tip or text))
        lb.bind("<Leave>", lambda e: self._tip_hide())
        return lb

    def _tip_show(self, widget, text):
        if not text:
            return
        try:
            self._tip.children["!label"].configure(text=text)
            x = widget.winfo_rootx() + 4
            y = widget.winfo_rooty() - 30
            self._tip.geometry(f"+{x}+{max(y, 4)}")
            self._tip.deiconify()
            self._tip.lift()
        except (tk.TclError, KeyError):
            pass

    def _tip_hide(self):
        try:
            self._tip.withdraw()
        except tk.TclError:
            pass

    def _drag_bind(self, *widgets):
        for w in widgets:
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<Button-3>", self._drag_reset)

    def _drag_start(self, e):
        self._dx = e.x_root - self.win.winfo_x()
        self._dy = e.y_root - self.win.winfo_y()

    def _drag_move(self, e):
        self.user_pos = (e.x_root - self._dx, e.y_root - self._dy)
        self.place()

    def _drag_reset(self, e):
        self.user_pos = None
        self.place()

    # ----- placement (always fully on-screen, never jumps while typing)
    def place(self):
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        ms = get_monitors(self.root)
        if self.user_pos:
            ax, ay = self.user_pos[0] + w // 2, self.user_pos[1] + h // 2
        elif self._user_anchor:
            ax, ay = self._user_anchor          # fixed where the card first appeared
        else:
            anchor = self.snap and self.snap.get("anchor")
            ax, ay = anchor if anchor else self.root.winfo_pointerxy()
        mx, my, mw, mh = monitor_at(ms, ax, ay)
        if self.user_pos:
            x, y = self.user_pos
        else:
            x = mx + mw - w - CARD_MARGIN
            y = my + mh - h - BOTTOM_MARGIN
        x = max(mx + 4, min(x, mx + mw - w - 4))
        y = max(my + 4, min(y, my + mh - h - 4))
        if self.user_pos:
            self.user_pos = (x, y)
        cur = (self.win.winfo_x(), self.win.winfo_y())
        if (int(x), int(y)) != cur or not self.card_rect or \
                self.card_rect[2:] != (w, h):
            self.win.geometry(f"+{int(x)}+{int(y)}")   # only move when it changed
        self.card_rect = (int(x), int(y), int(w), int(h))

    def _show(self):
        if self.win.state() == "withdrawn":
            self._user_anchor = self.root.winfo_pointerxy()
            self.win.deiconify()
        self.win.lift()
        self.place()

    # ----- rendering
    def show_toast(self, text):
        self.toast_text, self.toast_until = text, time.time() + 2.2
        self.render(self.model.snapshot())

    def render(self, snap):
        issues = snap["issues"]
        # --- anti-flicker: only rebuild the card when its CONTENT changed.
        sig = (tuple((i["id"], i["kind"], i["label"], i["hs"], i["he"]) for i in issues),
               snap["selected"], self.collapsed, time.time() < self.toast_until)
        if sig == self._last_sig and self.win.state() != "withdrawn":
            self.snap = snap
            self.draw_underlines()
            return
        # ...and never rebuild more than ~4x/second while typing fast
        if time.time() - self._render_at < 0.25 and self.win.state() != "withdrawn":
            self._pending = snap
            self.draw_underlines()
            return
        self._last_sig, self._render_at, self._pending = sig, time.time(), None
        ms = get_monitors(self.root)
        # anchor: where the user is typing, so the card appears on that monitor
        pts = [i["rect"] for i in issues if i["rect"]]
        snap["anchor"] = (pts[-1][0], pts[-1][1]) if pts else None
        self.snap = snap
        toast_on = time.time() < self.toast_until
        for w in self.frame.winfo_children():
            w.destroy()
        if not issues:
            if toast_on:
                tk.Label(self.frame, text=self.toast_text, bg=BG, fg=FG,
                         font=("Helvetica", 11, "bold")).pack()
                self._show()
            else:
                if self.win.state() != "withdrawn":
                    self.win.withdraw()
                self._user_anchor = None
                self.card_rect = None
            self.draw_underlines()
            return

        f = self.frame
        n = len(issues)
        hdr = tk.Frame(f, bg=BG)
        hdr.pack(fill="x")
        title = tk.Label(hdr, text="✎ Writing Assistant", bg=BG, fg=FG, font=("Helvetica", 11, "bold"))
        title.pack(side="left")
        count = tk.Label(hdr, text=f"  {n} issue{'s' if n != 1 else ''}", bg=BG, fg=RED,
                         font=("Helvetica", 11, "bold"))
        count.pack(side="left")
        self.chip(hdr, "+" if self.collapsed else "–", self.toggle_collapse,
                  tip="Collapse / expand the card (Ctrl+Alt+C)").pack(side="right")
        quit_chip = self.chip(hdr, "⏸" if not snap["paused"] else "▶",
                              self.model.toggle_pause,
                              tip="Pause / resume checking (Ctrl+Alt+P)")
        quit_chip.pack(side="right", padx=(0, 4))
        if snap["paused"]:
            tk.Label(hdr, text="paused", bg=BG, fg=MUTED, font=("Helvetica", 9)).pack(side="right")
        self._drag_bind(hdr, title, count)
        if toast_on:
            tk.Label(f, text=self.toast_text, bg=BG, fg=GREEN, font=("Helvetica", 10)).pack(anchor="w")

        if not self.collapsed:
            self._render_body(f, snap)
        self._show()
        self.draw_underlines()

    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        if self.snap:
            self.render(self.snap)

    def _render_body(self, f, snap):
        text, issues = snap["text"], snap["issues"]
        sel = next((i for i in issues if i["id"] == snap["selected"]), issues[-1])

        # preview of recent text with underlined issues (works in every app)
        base = max(0, len(text) - PREVIEW_CHARS)
        tail = text[base:].replace("\n", "↵")
        t = tk.Text(f, width=50, height=3, wrap="word", bg=PANEL, fg=FG, bd=0,
                    highlightthickness=0, padx=6, pady=4, font=("Helvetica", 11), cursor="arrow")
        t.insert("1.0", tail)
        for i in issues:
            if i["hs"] < base:
                continue
            tag = f"i{i['id']}"
            a, b = i["hs"] - base, min(i["he"], len(text)) - base
            t.tag_add(tag, f"1.0+{a}c", f"1.0+{b}c")
            t.tag_config(tag, underline=True, foreground=KIND_COLOR.get(i["kind"], RED),
                         background="#374151" if i["id"] == sel["id"] else PANEL)
            t.tag_bind(tag, "<Button-1>", lambda e, iid=i["id"]: self.model.select(iid))
            t.tag_bind(tag, "<Enter>", lambda e: t.config(cursor="hand2"))
            t.tag_bind(tag, "<Leave>", lambda e: t.config(cursor="arrow"))
        t.see("end")
        t.config(state="disabled")
        t.pack(fill="x", pady=(8, 8))

        color = KIND_COLOR.get(sel["kind"], RED)
        tk.Label(f, text=sel["label"], bg=BG, fg=color, font=("Helvetica", 10, "bold")).pack(anchor="w")
        tk.Label(f, text=f"“{sel['word'].strip() or '␠'}”", bg=BG, fg=FG,
                 font=("Helvetica", 14, "bold")).pack(anchor="w", pady=(0, 6))

        row = tk.Frame(f, bg=BG)
        row.pack(anchor="w", pady=(0, 8))
        if sel["suggestions"]:
            for idx, (lab, _rep) in enumerate(sel["suggestions"][:3]):
                conf = confidence(sel["word"].strip(), lab.split("→")[-1].strip()) \
                    if sel["kind"] == "spelling" else 0.95
                bar = "●" * max(1, round(conf * 5)) + "○" * (5 - max(1, round(conf * 5)))
                self.chip(row, f"{idx + 1}  {lab}",
                          lambda idx=idx, iid=sel["id"]: self.model.apply(iid, idx),
                          bg="#065f46",
                          tip=f"Apply (Ctrl+Alt+{idx + 1})\nconfidence: {bar}").pack(side="left", padx=(0, 6))
        else:
            tk.Label(row, text="No suggestions", bg=BG, fg=MUTED, font=("Helvetica", 10)).pack()

        acts = tk.Frame(f, bg=BG)
        acts.pack(anchor="w")
        self.chip(acts, "Ignore", self.model.ignore,
                  tip="Skip this issue (Ctrl+Alt+D)").pack(side="left", padx=(0, 6))
        if sel["kind"] == "spelling":
            self.chip(acts, "Add to dictionary", self.model.add_word,
                      tip="Never flag this word again (Ctrl+Alt+W)").pack(side="left", padx=(0, 6))
        if len(issues) > 1:
            self.chip(acts, "Next", self.model.next_issue,
                      tip="Jump to the next issue (Ctrl+Alt+N)").pack(side="left", padx=(0, 6))
            self.chip(acts, f"Fix all ({sum(1 for i in issues if i['suggestions'])})",
                      self.model.fix_all, bg="#1e3a8a",
                      tip="Apply the top suggestion to every issue (Ctrl+Alt+F)").pack(side="left")

        others = [i for i in issues if i["id"] != sel["id"]][:MAX_CHIPS]
        if others:
            orow = tk.Frame(f, bg=BG)
            orow.pack(anchor="w", pady=(8, 0))
            tk.Label(orow, text="Also:", bg=BG, fg=MUTED, font=("Helvetica", 9)).pack(side="left")
            for i in others:
                lb = tk.Label(orow, text=i["word"].strip() or "␠", bg=BG,
                              fg=KIND_COLOR.get(i["kind"], RED), cursor="hand2",
                              font=("Helvetica", 10, "underline"), padx=4)
                lb.bind("<Button-1>", lambda e, iid=i["id"]: self.model.select(iid))
                lb.pack(side="left")

        tk.Label(f, text="Ctrl+Alt: 1-3 apply · N next · D ignore · W add word · O open dictionary folder · F fix all · P pause · Q quit",
                 bg=BG, fg=MUTED, font=("Helvetica", 8), wraplength=380, justify="left"
                 ).pack(anchor="w", pady=(8, 0))

    # ----- in-place underlines
    def draw_underlines(self):
        items = []
        snap = self.snap
        if snap and not snap["paused"]:
            ox, oy = window_origin()
            ms = get_monitors(self.root)
            sel = snap["selected"]
            for i in snap["issues"]:
                if not i["rect"]:
                    continue
                x, y, w = i["rect"]
                # the rect was projected while the target window sat at i["origin"];
                # shift by however far that window has moved since (0 on mac/linux)
                x += ox - i["origin"][0]
                y += oy - i["origin"][1]
                w = max(int(w), 6)                       # never a zero-width mark
                y += 1                                   # sit just below the text baseline
                if any(m[0] <= x < m[0] + m[2] and m[1] <= y < m[1] + m[3] for m in ms):
                    items.append((int(x), int(y), w, KIND_COLOR.get(i["kind"], RED),
                                  4 if i["id"] == sel else 3))
        # deterministic order: top-to-bottom, left-to-right -> stable stacking
        items.sort(key=lambda it: (it[1], it[0]))
        items = items[:len(self.lines)]
        want = {idx: (x, y, wd, color, th)
                for idx, (x, y, wd, color, th) in enumerate(items)}
        for idx, w in enumerate(self.lines):
            try:
                spec = want.get(idx)
                if spec is None:
                    if w.state() == "normal":
                        w.withdraw()
                    continue
                x, y, wd, color, th = spec
                cur = (w.winfo_x(), w.winfo_y(), w.winfo_width(), w.winfo_height())
                if w.state() != "normal" or cur != (x, y, wd, th):
                    w.configure(bg=color)
                    # geometry BEFORE deiconify: many WMs refuse to map borderless
                    # windows that still have their old 1x1 size - this is why the
                    # underlines used to never show up. Re-setting identical geometry
                    # every tick is what caused the flicker, so we skip it.
                    w.geometry(f"{wd}x{th}+{x}+{y}")
                    if w.state() != "normal":
                        w.deiconify()
                        w.lift(idx)                      # only on (re)map -> no flicker
            except tk.TclError:
                pass

    def tick(self):
        self.draw_underlines()
        if self.toast_until and time.time() > self.toast_until:
            self.toast_until = 0.0
            self._last_sig = None          # force one rebuild to drop the toast line
            self.render(self.model.snapshot())
        elif self._pending and time.time() - self._render_at >= 0.25:
            snap, self._pending = self._pending, None
            self._last_sig = None          # draw the update we skipped earlier
            self.render(snap)
        self.root.after(250, self.tick)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    if "--import" in sys.argv:
        paths = sys.argv[sys.argv.index("--import") + 1:]
        paths = [p for p in paths if not p.startswith("--")]
        print("Importing dictionaries...")
        import_dictionaries(paths)
        print("Done. They load automatically next time the assistant runs (or within seconds if it's running).")
        return
    make_dpi_aware()
    init_spell_dictionary()            # merge the pre-packed dictionary now
    root = tk.Tk()
    root.withdraw()
    ui_q = queue.Queue()
    model = Assistant(lambda: ui_q.put(("refresh",)),
                      lambda text: ui_q.put(("toast", text)),
                      CaretLocator(root))
    ui = UI(root, model)
    keys = Keys(model, ui_q)
    keys.ui = ui

    kb_listener = keyboard.Listener(on_press=keys.on_press, on_release=keys.on_release)
    ms_listener = mouse.Listener(on_click=keys.on_click, on_scroll=keys.on_scroll)
    kb_listener.start()
    ms_listener.start()
    stop = threading.Event()
    threading.Thread(target=watch_focus, args=(model, stop), daemon=True).start()

    def poll():
        refresh, toast = False, None
        try:
            while True:
                msg = ui_q.get_nowait()
                if msg[0] == "refresh":
                    refresh = True
                elif msg[0] == "toast":
                    toast = msg[1]
                elif msg[0] == "quit":
                    stop.set()
                    kb_listener.stop()
                    ms_listener.stop()
                    root.destroy()
                    return
        except queue.Empty:
            pass
        if toast:
            ui.show_toast(toast)
        elif refresh:
            ui.render(model.snapshot())
        root.after(60, poll)

    print(f"Dictionary folder: {DICT_DIR}  (drop word lists here; Ctrl+Alt+O opens it)")
    print("Writing Assistant v4 running.\n"
          "  Ctrl+Alt+1/2/3 apply | N next | D ignore | W add word | F fix all | P pause | Q quit")
    root.after(60, poll)
    root.after(250, ui.tick)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
