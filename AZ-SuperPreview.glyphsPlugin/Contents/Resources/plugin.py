# AZ Super Preview: General Plugin for Glyphs.app (3 and 4).
# Shows every instance of the family, or its masters, on its own card at one point
# size; instances are interpolated live.


import objc
import re
import math
import json
import socket
import threading
import traceback
import time
from urllib.parse import parse_qs
from typing import Any

from GlyphsApp import (
    Glyphs,
    DOCUMENTACTIVATED,
    UPDATEINTERFACE,
    WINDOW_MENU,
    OFFCURVE,
)
from GlyphsApp.plugins import GeneralPlugin

from vanilla import (
    FloatingWindow,
    EditText,
    Button,
    Slider,
    TextBox,
    CheckBox,
    Stepper,
    Group,
    SplitView,
    SegmentedButton,
    VanillaBaseObject,
    Popover,
    ImageView,
    PopUpButton,
)
from AppKit import (
    NSMenuItem,
    NSAffineTransform,
    NSView,
    NSScrollView,
    NSColor,
    NSBezierPath,
    NSAttributedString,
    NSMutableAttributedString,
    NSFont,
    NSMutableParagraphStyle,
    NSGraphicsContext,
    NSParagraphStyleAttributeName,
    NSForegroundColorAttributeName,
    NSFontAttributeName,
    NSViewWidthSizable,
    NSViewController,
    NSLineBreakByTruncatingTail,
    NSCursor,
    NSEventModifierFlagShift,
    NSTrackingArea,
    NSTrackingMouseMoved,
    NSTrackingMouseEnteredAndExited,
    NSTrackingActiveInKeyWindow,
    NSTrackingInVisibleRect,
    NSImage,
    NSImageOnly,
    NSButton,
    NSMenu,
    NSViewHeightSizable,
    NSViewMaxXMargin,
    NSViewMinXMargin,
    NSViewMinYMargin,
    NSViewMaxYMargin,
    NSGradient,
    NSColorSpace,
    NSEvenOddWindingRule,
)
from Foundation import (
    NSMakeRect,
    NSMakeSize,
    NSObject,
    NSInsetRect,
    NSRunLoopCommonModes,
    NSData,
    NSTimer,
    NSRunLoop,
    NSUserDefaults,
)
from collections import OrderedDict

LIVE_REDRAW_DELAY = 0.12  # s; coalesces outline-edit redraws
ORDER_MEMO_TTL = 1.0  # s a frame reuses sorted rows + keys; Font Info edits show after
GRAB_HANDLE_W = 13.0  # row-label width reserved for the drag grip
CARD_INK_LEFT_PAD = 6.0  # min px from card edge to leftmost ink; keeps italic feet
MIN_WINDOW_H = 300  # frame floor; _applyMinHeight enforces CONTENT_MIN_H
CONTENT_MIN_H = 600  # min floating-window content height, both modes
ROW_GAP = 13  # fixed row/column spacing (font pts)
ROW_CARD_PAD = 4.0  # row card outset around its cell, under ROW_GAP / 2
SB_RULE_WIDTH = 0.6  # shared by sidebearing rules and the outline x-ray
SPACING_LINE_H = 13.0  # one value/name text line beneath each glyph
ROW_INK_PAD = 12.0  # pt above ascender / below descender; waterfall scales it
SPACING_DESC_GAP_UNITS = 20.0  # font units below the descender before the spacing line
try:  # small monospaced, slightly bolder font for the spacing values
    from AppKit import NSFontWeightMedium

    OVERLAY_VALUE_FONT = NSFont.monospacedSystemFontOfSize_weight_(
        8.5, NSFontWeightMedium
    )
except Exception:
    OVERLAY_VALUE_FONT = NSFont.systemFontOfSize_(8.5)
MARKER_REF_SIZE = 160.0  # pt size at which node/anchor markers draw at base screen size
TOGGLE_BTN_W = 30.0  # toolbar icon buttons (sidebar switch, Remote, View)
ACTIVE_ONLY_MENU_TAG = (
    7701  # lets the mode segment add/remove the Active-only item live
)
# (preview attribute, title, indent) in popover / menu order; the defaults key is the
# attribute without its underscore (".showGuides")
VIEW_OPTIONS = (
    ("_showCoords", "Show axes value", 0),
    ("_showGuides", "Show metrics", 0),
    ("_showZones", "Show alignment zones", 1),
    ("_showKerning", "Show kerning", 0),
    ("_showSpacing", "Show spacing", 0),
    ("_showOutlines", "Show outlines", 0),
    ("_showNodes", "Show nodes", 1),
    ("_showAnchors", "Show anchors", 1),
)
SORT_ROW_MENU_TAG = 7702  # lets a click find and re-render the sort-priority rows

# Glyphs 4 renamed LTR/RTL to GSLTR/GSRTL (importing LTR raises ImportError there);
# fall back to the GSWritingDirection ints (LeftToRight=0, RightToLeft=2).
try:
    from GlyphsApp import LTR
except Exception:
    LTR = 0
try:
    from GlyphsApp import RTL
except Exception:
    RTL = 2

# Variable Font Setting instances (type 1) have no single interpolated output: skipped.
try:
    from GlyphsApp import INSTANCETYPEVARIABLE
except Exception:
    INSTANCETYPEVARIABLE = 1

PLUGIN_ID = "com.hugojourdan.AZSuperPreview"

# ---------------------------------------------------------------------------- #
#  AZ suite — sidebar style tokens shared by every AZ plugin with a Glyphs 4 sidebar
#  tab, so some are unused here.
# ---------------------------------------------------------------------------- #
AZ_CARD_RADIUS = 8.0  # rounded corner of a sidebar card
AZ_CHIP_RADIUS = 6.0  # inner chips, pills and grid cells
AZ_SIDE_MARGIN = 10.0  # sidebar edge -> card
AZ_CARD_PAD_X = 8.0  # card edge -> content, horizontal
AZ_CARD_PAD_Y = 6.0  # card edge -> content, vertical
AZ_CARD_GAP = 6.0  # between stacked cards
AZ_BAND_H = 24.0  # card header band height
AZ_TEXT_PT = 11.0  # body and (bold) header type
AZ_METRIC_PT = 9.0  # monospaced metric / caption type
AZ_TAB_ICON_H = 17.6  # sidebar tab-bar icon height (16 + 10%)
AZ_CARD_RGB = 40 / 255.0  # house card grey, ALWAYS declared in sRGB
# Text ON a card. The card is PINNED dark (it does not follow the appearance), so
# its text must be pinned too: labelColor & friends resolve to near-black in light
# mode and go dark-on-dark. White at a fixed alpha, never a system label colour.
AZ_INK = 0.95  # primary card text
AZ_INK_DIM = 0.62  # captions, counts, secondary card text


def _az_ink():
    """Primary text on a pinned card — white at AZ_INK, never a system label colour."""
    return NSColor.whiteColor().colorWithAlphaComponent_(AZ_INK)


def _az_ink_dim():
    """Secondary/caption text on a pinned card."""
    return NSColor.whiteColor().colorWithAlphaComponent_(AZ_INK_DIM)


def _az_card_fill():
    """The house card grey (RGB 40,40,40) in sRGB.

    Must never be `colorWithCalibratedRed_`: the calibrated space is gamma 1.8,
    so a calibrated 40 paints as sRGB ~53 and the card reads visibly lighter
    than its neighbours in other AZ plugins."""
    return NSColor.colorWithSRGBRed_green_blue_alpha_(
        AZ_CARD_RGB, AZ_CARD_RGB, AZ_CARD_RGB, 1.0
    )


def _az_draw_glass_border(rect, radius):
    # macOS-Glass-like rim: a subtle 1pt ring lit diagonally — brighter at the
    # top-left and bottom-right corners, darker at top-right and bottom-left. A
    # 3-stop gradient (light -> dark -> light) along the TL->BR diagonal lands
    # both off-diagonal corners on the dark midpoint. Flip-aware via the context.
    outer = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        rect, radius, radius
    )
    inner = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSInsetRect(rect, 1.0, 1.0), max(0.0, radius - 1.0), max(0.0, radius - 1.0)
    )
    ring = NSBezierPath.bezierPath()
    ring.appendBezierPath_(outer)
    ring.appendBezierPath_(inner)
    ring.setWindingRule_(NSEvenOddWindingRule)
    context = NSGraphicsContext.currentContext()
    if context is None:
        return
    context.saveGraphicsState()
    try:
        ring.addClip()
        light = NSColor.whiteColor().colorWithAlphaComponent_(0.32)
        dark = _az_card_fill()  # card colour, not black -> dark stops blend in
        gradient = NSGradient.alloc().initWithColors_atLocations_colorSpace_(
            [light, dark, dark, light],
            [0.0, 0.375, 0.625, 1.0],
            NSColorSpace.genericRGBColorSpace(),
        )
        # A FIXED 45° lighting angle, not the corner-to-corner diagonal (whose angle
        # would tilt with the card's aspect ratio and look inconsistent).
        gradient.drawInRect_angle_(rect, 45.0 if bool(context.isFlipped()) else 135.0)
    finally:
        context.restoreGraphicsState()


# The settings and preview cards each trim their facing edge to half AZ_CARD_GAP.
# Local, not AZ_*: the AZ_ tokens are suite-shared.
_CARD_FACING_INSET = AZ_CARD_GAP / 2.0
# Extra air in this plugin's settings card only; AZ_CARD_PAD_Y is suite-shared.
_CARD_PAD_Y_EXTRA = 4.0
# NSPopUpButton draws its bezel ~3pt outside its frame; inset so it aligns with fields.
POPUP_BEZEL = 3.0


def _az_draw_card(view, insetTop, insetBottom):
    """House card (fill + glass rim) inset from `view` bounds; top/bottom insets let a
    card trim the edge facing its neighbour. Not flipped: origin.y is the BOTTOM."""
    bounds = view.bounds()
    rect = NSMakeRect(
        bounds.origin.x + AZ_SIDE_MARGIN,
        bounds.origin.y + insetBottom,
        bounds.size.width - 2.0 * AZ_SIDE_MARGIN,
        bounds.size.height - insetTop - insetBottom,
    )
    if rect.size.width <= 1.0 or rect.size.height <= 1.0:
        return
    _az_card_fill().set()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        rect, AZ_CARD_RADIUS, AZ_CARD_RADIUS
    ).fill()
    _az_draw_glass_border(rect, AZ_CARD_RADIUS)


class AZSPSettingsCardView(NSView):
    """House card behind the sidebar settings band. Must be gS's FIRST subview so the
    controls draw on top; hidden in the floating window (thin toolbar, no card)."""

    def drawRect_(self, dirty):
        _az_draw_card(self, AZ_SIDE_MARGIN, _CARD_FACING_INSET)


class AZSPPreviewCardView(AZSPSettingsCardView):
    """House card behind the preview pane, below the settings card: trims its top."""

    def drawRect_(self, dirty):
        _az_draw_card(self, _CARD_FACING_INSET, AZ_SIDE_MARGIN)


_TAB_TITLE = "AZ Super Preview"
# Tab-bar NSImage, built once (Glyphs asks +tabBarImage per Edit view). Module-level:
# PyObjC classes reject arbitrary Python attributes.
_TAB_IMAGE_CACHE = []


WATERFALL_START = 12  # default first (smallest) Waterfall size (pt)
WATERFALL_STEPS = 17  # default number of Waterfall sizes
WATERFALL_GROWTH = 1.13  # each size ~13% above the previous
WATERFALL_GAP = 10.4  # px between ladder rows
WATERFALL_TOP_INSET = 10.0  # px above the first row
WATERFALL_BOTTOM_PAD = 10.0  # px below the last row
WATERFALL_COLUMN_W = 500.0  # Multi columns: one more column per this many pt of width
TOOLBAR_H = (
    46.0  # floating: fixed height of the settings bar above the preview, every mode
)


def waterfallLayout(extents, availH, columns):
    """(column, top y) per ladder row, plus the content height. Rows that would end below
    availH continue at the top of the next column; the last column scrolls."""
    rows = []
    bottoms = [WATERFALL_TOP_INSET] * columns
    col, y = 0, WATERFALL_TOP_INSET
    for ext in extents:
        if (
            col < columns - 1
            and y > WATERFALL_TOP_INSET  # a column's first row never moves on
            and y + ext + WATERFALL_GAP + WATERFALL_BOTTOM_PAD > availH
        ):
            col, y = col + 1, WATERFALL_TOP_INSET
        rows.append((col, y))
        y += ext + WATERFALL_GAP
        bottoms[col] = y
    return rows, max(bottoms) + WATERFALL_BOTTOM_PAD


def _finiteFloat(text):
    """A typed number, or None when the text isn't one (empty, mid-edit, nan, inf)."""
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def waterfallSizes(start, steps):
    """The Waterfall ladder: start (4-200 pt), then each size ~13% above the previous,
    rounded to an even size below 20 pt and to a multiple of 4 from 20 pt up."""
    size = max(4, min(200, int(round(start))))
    sizes = [size]
    for _ in range(max(1, min(40, int(round(steps)))) - 1):
        target = size * WATERFALL_GROWTH
        grid = 2 if target < 20 else 4
        # at least one grid step up: small sizes would otherwise round back down
        size = max(math.floor(target / grid + 0.5) * grid, (size // grid + 1) * grid)
        sizes.append(size)
    return sizes


SF_COLUMN = "􀏟"  # column-count control label

# System-symbol names ship in macOS 11+, so they render without SF Pro installed; the
# bare SF characters only resolve when font fallback finds SF Pro.
SFNAME_COLUMN = "rectangle.split.3x1"
SFNAME_PANEL = "rectangle.grid.2x2.fill"
SFNAME_VIEW = "paintbrush.fill"  # View options button
SFNAME_SWAP = "rectangle.2.swap"  # sidebar <-> floating; macOS 12+, as Glyphs 4


def symbolImage(name, pointSize):
    """Template NSImage for an SF Symbol name; None when macOS lacks it (or pre-11)."""
    try:
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    except Exception:
        img = None
    if img is None:
        return None
    try:
        from AppKit import (
            NSImageSymbolConfiguration,
            NSFontWeightRegular,
            NSImageSymbolScaleMedium,
        )

        cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
            pointSize, NSFontWeightRegular, NSImageSymbolScaleMedium
        )
        scaled = img.imageWithSymbolConfiguration_(cfg)
        if scaled is not None:
            img = scaled
    except Exception:
        pass
    img.setTemplate_(True)  # tint with the control (light/dark aware)
    return img


def setButtonImage(button, img, fallbackChar):
    """`img` on a vanilla Button, else the raw character as its title."""
    nsb = button._nsObject
    if img is not None:
        nsb.setImage_(img)
        nsb.setImagePosition_(NSImageOnly)
        nsb.setTitle_("")
    else:
        nsb.setImage_(None)
        nsb.setTitle_(fallbackChar)


# Remote page: polls /seq at 2 Hz, refetches the PDF snapshot on change (PNG off WebKit).
REMOTE_PAGE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<title>AZ Super Preview</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;padding:0;background:#161616;color:#ddd;
 font-family:-apple-system,system-ui,sans-serif;min-height:100%}
#bar{position:sticky;top:0;z-index:9;
 padding:max(8px,env(safe-area-inset-top)) max(12px,env(safe-area-inset-right))
  8px max(12px,env(safe-area-inset-left));
 background:rgba(28,28,30,.88);
 -webkit-backdrop-filter:blur(16px);backdrop-filter:blur(16px)}
#row1{display:flex;gap:8px;align-items:stretch}
.seg{display:flex;flex:1;background:rgba(120,120,128,.24);border-radius:7px;padding:2px}
.seg button{flex:1;min-height:24px;border:0;border-radius:5px;background:transparent;
 color:#ddd;font-size:12px;font-weight:500;touch-action:manipulation}
.seg button.on{background:#636366;color:#fff}
#bReload{flex:0 0 34px;min-height:26px;border:0;border-radius:7px;
 background:rgba(120,120,128,.24);color:#ddd;font-size:14px;touch-action:manipulation}
#mctl{margin-top:8px}
#mtext{width:100%;min-height:30px;margin-bottom:8px;border:0;border-radius:8px;
 padding:0 10px;background:rgba(120,120,128,.24);color:#ddd;font-size:14px;
 -webkit-appearance:none}
.mrow{display:flex;gap:10px;align-items:center;min-height:36px}
.mrow input{flex:1;min-height:36px}
.mlb{min-width:56px;font-size:13px;color:#bbb;white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis}
.mvl{min-width:44px;text-align:right;font-size:13px;font-variant-numeric:tabular-nums}
img{display:block;width:100%;height:auto}
#off{display:none;position:fixed;inset:0;z-index:9999;flex-direction:column;align-items:center;justify-content:center;gap:8px;text-align:center;padding:24px;background:#111;color:#eee;font:600 20px -apple-system,system-ui,sans-serif}#off.show{display:flex}#off .sub{font-weight:400;font-size:14px;color:#9a9a9a;max-width:320px;line-height:1.45}
</style></head><body>
<div id="bar">
 <div id="row1">
  <div class="seg" id="segMode">
   <button>Overview</button><button>Waterfall</button>
  </div>
  <button id="bReload">&#8635;</button>
 </div>
 <div id="mctl">
  <input id="mtext" type="text" placeholder="Sample text…" autocapitalize="off" autocomplete="off" spellcheck="false">
  <div class="mrow"><span class="mlb">Scale</span><input id="msize"
   type="range" min="1" max="4" step="0.1" value="1"><span class="mvl" id="msPt">×1</span></div>
 </div>
</div>
<img id="p" alt="">
<div id="off"><div>Preview disconnected</div><div class="sub">Reconnecting… make sure Glyphs is running with AZ&nbsp;Super&nbsp;Preview open.</div></div>
<script>
var img=document.getElementById("p");
var off=document.getElementById("off"),netFails=0,offline=false;
var usePng=false,seq="",phMode=0,stInit=false,curState=null;
var modeBtns=document.getElementById("segMode").getElementsByTagName("button");
var msize=document.getElementById("msize"),msPt=document.getElementById("msPt");
var mtext=document.getElementById("mtext");
function markMode(mi){
 for(var i=0;i<modeBtns.length;i++)modeBtns[i].className=(i===mi)?"on":"";
}
function applyState(st){
 curState=st;
 if(typeof st.mode==="number"){phMode=st.mode;markMode(phMode);}
 if(typeof st.text==="string"&&document.activeElement!==mtext)mtext.value=st.text;
 configSizeRow();
}
function fetchState(){
 fetch("/state",{cache:"no-store"}).then(function(r){return r.json();}).then(applyState).catch(function(){});
}
function configSizeRow(){
 var v=(curState&&(phMode===1?curState.wfmul:curState.ovmul))||1;
 msize.value=v;msPt.textContent="×"+(Math.round(v*10)/10);
}
msize.oninput=function(){
 var v=Math.round(msize.value*10)/10;msPt.textContent="×"+v;
 fetch("/set?"+(phMode===1?"wf":"ov")+"="+v,{cache:"no-store"}).catch(function(){});
};
mtext.oninput=function(){
 fetch("/set?text="+encodeURIComponent(mtext.value),{cache:"no-store"}).catch(function(){});
};
for(var i=0;i<modeBtns.length;i++)(function(n){
 modeBtns[n].onclick=function(){
  phMode=n;markMode(n);configSizeRow();
  fetch("/mode?m="+n,{cache:"no-store"}).catch(function(){});
  setTimeout(fetchState,700);
 };
})(i);
img.onerror=function(){
 if(!usePng){usePng=true;if(seq)img.src="/preview.png?"+seq;}
};
function goOffline(){
 if(offline)return;offline=true;
 img.removeAttribute("src");
 off.className="show";
}
function tick(){
 fetch("/seq",{cache:"no-store"}).then(function(r){
  if(!r.ok)throw 0;return r.text();
 }).then(function(s){
  if(offline){location.reload();return;}
  netFails=0;
  var parts=s.split(":"),sq=parts[0],mi=parseInt(parts[1],10);
  if(mi>=0&&mi<=1&&mi!==phMode){phMode=mi;markMode(mi);configSizeRow();}
  if(sq!==seq&&sq!=="0"){seq=sq;
   img.src=(usePng?"/preview.png?":"/preview.pdf?")+sq;
   if(!stInit){stInit=true;fetchState();}}
 }).catch(function(){
  if(++netFails>=2)goOffline();
 }).then(function(){setTimeout(tick,500);});
}
tick();
fetchState();
document.getElementById("bReload").onclick=function(){
 if(seq)img.src=(usePng?"/preview.png?":"/preview.pdf?")+seq+"&r="+Date.now();
};
</script></body></html>"""

# NSView PDFs stamp fresh dates and a random /ID into every capture
PDF_VOLATILE = re.compile(rb"/(?:CreationDate|ModDate)\s*\([^)]*\)|/ID\s*\[[^\]]*\]")


def lanIP():
    """Best-guess LAN address: a UDP connect() picks the outbound interface without
    sending a packet. Localhost when the Mac has no route."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 80))  # TEST-NET, never contacted
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


def makeQRImage(text, pointSize):
    """QR NSImage via CoreImage's CIQRCodeGenerator, upscaled with interpolation off so
    modules stay crisp. None without CoreImage."""
    try:
        CIFilter = objc.lookUpClass("CIFilter")
        NSCIImageRep = objc.lookUpClass("NSCIImageRep")
        flt = CIFilter.filterWithName_("CIQRCodeGenerator")
        if flt is None:
            return None
        raw = text.encode("utf-8")
        flt.setValue_forKey_(
            NSData.dataWithBytes_length_(raw, len(raw)), "inputMessage"
        )
        flt.setValue_forKey_("M", "inputCorrectionLevel")
        ci = flt.valueForKey_("outputImage")
        if ci is None:
            return None
        ext = ci.extent()
        rep = NSCIImageRep.imageRepWithCIImage_(ci)
        small = NSImage.alloc().initWithSize_(ext.size)
        small.addRepresentation_(rep)
        img = NSImage.alloc().initWithSize_(NSMakeSize(pointSize, pointSize))
        img.lockFocus()
        try:
            NSGraphicsContext.currentContext().setImageInterpolation_(1)  # 1 = none
            small.drawInRect_fromRect_operation_fraction_(
                NSMakeRect(0, 0, pointSize, pointSize),
                NSMakeRect(0, 0, ext.size.width, ext.size.height),
                1,
                1.0,
            )  # 1 = copy
        finally:
            img.unlockFocus()
        return img
    except Exception:
        return None


def iconLabel(posSize, name, fallbackChar, pointSize=13.0):
    """ImageView with an SF Symbol, or a TextBox with the raw character as fallback."""
    img = symbolImage(name, pointSize)
    if img is None:
        return TextBox(posSize, fallbackChar, sizeStyle="regular")
    view = ImageView(posSize, scale="proportional")
    view.setImage(imageObject=img)
    try:
        view._nsObject.setContentTintColor_(NSColor.labelColor())  # match label text
    except Exception:
        pass
    return view


def _azSizeStepDoCommand(self, control, textView, commandSelector):
    # Module-level, else PyObjC 12 registers it as a 0-arg selector (BadPrototypeError).
    # Wrapped below with a BOOL/SEL signature: the informal protocol isn't declared.
    try:
        return bool(self._owner._sizeFieldArrowStep())
    except Exception:
        return False


class AZSizeStepDelegate(NSObject):
    """Size field delegate: Up/Down step the value (Shift = ±10). Replaces the field's
    delegate, so it re-forwards change / end-editing to the size callback."""

    def initWithOwner_(self, owner):
        self = objc.super(AZSizeStepDelegate, self).init()
        if self is None:
            return None
        self._owner = owner
        return self

    def controlTextDidChange_(self, note):
        try:
            self._owner.sizeTextChanged_(self._owner.gS.sizeText)
        except Exception:
            pass

    def controlTextDidEndEditing_(self, note):
        try:
            self._owner.sizeTextChanged_(self._owner.gS.sizeText)
        except Exception:
            pass

    control_textView_doCommandBySelector_ = objc.selector(
        _azSizeStepDoCommand,
        selector=b"control:textView:doCommandBySelector:",
        signature=objc._C_NSBOOL
        + objc._C_ID
        + objc._C_SEL
        + objc._C_ID
        + objc._C_ID
        + objc._C_SEL,
    )


# variation selectors and skin-tone modifiers style the previous emoji
emoji_modifier = re.compile("[\ufe00-\ufe0f\U0001f3fb-\U0001f3ff]")


def getKernValue(layer1, layer2, rtl=False):
    """Kerning between two layers; 0 for no pair (Glyphs answers None or a huge
    sentinel)."""
    value = layer1.nextKerningForLayer_direction_(layer2, RTL if rtl else LTR)
    if value is None or value > 10000:
        return 0
    return value


def objectKey(obj):
    """Per-object cache key: the ObjC pointer, else Python's id()."""
    try:
        return objc.pyobjc_id(obj)
    except Exception:
        return id(obj)


def fmtNumber(value):
    """Axis value without a trailing .0 when integral."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f == int(f):
        return str(int(f))
    return f"{f:g}"


def isVariableInstance(instance):
    try:
        return int(instance.type) == INSTANCETYPEVARIABLE
    except Exception:
        return False


def instanceIsActive(instance):
    """Exports 'Active' checkbox; True when unreadable so no quirk hides every row."""
    try:
        return bool(instance.active)
    except Exception:
        return True


def staticInstances(font, activeOnly=False):
    """Instances minus Variable Font Settings; activeOnly also drops inactive ones."""
    if not font:
        return []
    insts = [i for i in font.instances if not isVariableInstance(i)]
    if activeOnly:
        insts = [i for i in insts if instanceIsActive(i)]
    return insts


def itemAxes(item):
    """Axis values of an instance or master: .axes (3.2+), else internalAxesValues."""
    try:
        return list(item.axes)
    except Exception:
        try:
            return list(item.internalAxesValues)
        except Exception:
            return []


def instanceIdentity(instance):
    """(familyName, name, axis coords). name alone collides in width families, where
    the width lives in familyName and every style is e.g. "Light"."""
    name = instance.name or ""
    try:
        family = instance.familyName or ""
    except Exception:
        family = ""
    return (family, name, tuple(itemAxes(instance)))


def instanceIdentityStr(instance):
    """instanceIdentity as a stable string, for persistence and custom order."""
    family, name, coords = instanceIdentity(instance)
    return "\x1f".join([family, name, ",".join(f"{c:g}" for c in coords)])


def displayItems(font, showMasters, activeOnly=False):
    """Rows to show: masters (drawn directly) or static instances (interpolated)."""
    if not font:
        return []
    if showMasters:
        return list(font.masters)
    return staticInstances(font, activeOnly)


def familyResidue(family, font):
    """familyName minus the typeface name, which repeats on every row
    ("ABC Social Mono" → "Mono", "ABC Social" → "")."""
    family = (family or "").strip()
    try:
        base = (font.familyName or "").strip()
    except Exception:
        base = ""
    if not family or not base:
        return family
    if family == base:
        return ""
    if family.startswith(base + " "):
        return family[len(base) + 1 :]
    return family


def itemKey(item, showMasters):
    """Stable key for order / selection; masters are prefixed so one saved order can
    hold both modes without collisions."""
    if showMasters:
        try:
            return "master:" + item.id
        except Exception:
            return "master:?"
    return instanceIdentityStr(item)


# Column-major grid geometry shared by drawing and drag hit-testing. _gridLayout is
# (keys, columns, numRows, colW, colGap, rowPitch, topMargin, leftMargin).
def gridColRow(index, numRows):
    col = index // numRows
    return col, index - col * numRows


def gridCellX(col, colW, colGap, leftMargin):
    return leftMargin + col * (colW + colGap)


def gridIndexAtPoint(
    px, py, keysLen, columns, numRows, colW, colGap, rowPitch, topMargin, leftMargin
):
    """Flat index of the cell under a point, or None (drag pick-up)."""
    if numRows <= 0 or py < topMargin:
        return None
    row = int((py - topMargin) // rowPitch)
    if row < 0 or row >= numRows:
        return None
    step = colW + colGap
    col = int((px - leftMargin) // step) if step > 0 else 0
    col = max(0, min(columns - 1, col))
    idx = col * numRows + row
    if idx >= keysLen:
        return None
    return idx


def gridSlotAtPoint(
    px, py, keysLen, columns, numRows, colW, colGap, rowPitch, topMargin, leftMargin
):
    """Insertion slot (0..keysLen) nearest the drag point; keysLen appends."""
    if numRows <= 0:
        return None
    step = colW + colGap
    col = int((px - leftMargin) // step) if step > 0 else 0
    col = max(0, min(columns - 1, col))
    rel = (py - topMargin) / rowPitch if rowPitch > 0 else 0
    row = int(rel + 0.5)
    row = max(0, min(numRows, row))
    return min(keysLen, col * numRows + row)


def reorderKeys(keys, src, dst):
    """Move item src to insertion slot dst (an index into the ORIGINAL list)."""
    keys = list(keys)
    if not (0 <= src < len(keys)):
        return keys
    if dst == src or dst == src + 1:
        return keys
    moved = keys.pop(src)
    if dst > src:
        dst -= 1
    dst = max(0, min(len(keys), dst))
    keys.insert(dst, moved)
    return keys


def dropIndicatorCell(src, dst, n):
    """(finalCell, append): finalCell is the dragged item's landing index; append
    (dst past the last slot) means draw the line at finalCell's bottom, not top."""
    append = dst >= n
    final = dst - (1 if dst > src else 0)
    final = max(0, min(n - 1, final))
    return final, append


def glyphsKernColour(positive, dark):
    """Glyphs' Positive/Negative Kerning colour (Settings ▸ Appearance), *Dark on dark.
    colorForKey_ is Glyphs' NSUserDefaults category (~6 µs, fine per frame)."""
    key = f"GSColorKerning{'Positive' if positive else 'Negative'}{'Dark' if dark else ''}"
    try:
        colour = NSUserDefaults.standardUserDefaults().colorForKey_(key)
    except Exception:
        colour = None
    return colour or (
        NSColor.systemYellowColor() if positive else NSColor.systemBlueColor()
    ).colorWithAlphaComponent_(0.2)


def kernBarSpan(seam, kernValue):
    """Font-unit (x0, width, positive) of the kern bar at `seam` (leading advance edge):
    |kern| wide, over the overlap when negative, over the added gap when positive."""
    return seam + min(0.0, kernValue), abs(kernValue), kernValue > 0


class LRUDict(OrderedDict):
    """Size-bounded dict with LRU eviction, so interpolation/path caches stay small."""

    def __init__(self, cap):
        super().__init__()
        self._cap = max(1, int(cap))

    def get(self, key, default=None):
        if key in self:
            self.move_to_end(key)
            return self[key]
        return default

    def __setitem__(self, key, value):
        if key in self:
            super().__setitem__(key, value)
            self.move_to_end(key)
        else:
            super().__setitem__(key, value)
            while len(self) > self._cap:
                self.popitem(last=False)


# The drawing view; flipped so rows stack top-down and the scroll view anchors at top.
class AZSuperPreviewView(NSView):
    wrapper: Any

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        # the click that focuses the panel should also act (lock/drag/activate)
        return True

    def viewDidChangeEffectiveAppearance(self):
        # Glyphs switched light/dark (view follows its window, which follows the app)
        try:
            objc.super(AZSuperPreviewView, self).viewDidChangeEffectiveAppearance()
        except Exception:
            pass
        try:
            wrapper = getattr(self, "wrapper", None)
            cb = wrapper.appearanceCallback if wrapper else None
            if cb is not None:
                cb()
        except Exception:
            pass

    def menuForEvent_(self, event):
        try:
            wrapper = getattr(self, "wrapper", None)
            target = wrapper._plugin if wrapper else None
            if target is None:
                return None
            menu = NSMenu.alloc().init()
            # Masters/Instances segment first (not in Waterfall, which forces the mode);
            # modeSegMenu_ adds/removes the Active-only item live when it toggles.
            try:
                if (
                    target._narrowHosted()  # floating has these in the top bar
                    and not bool(wrapper._waterfall)
                ):
                    target._addModeSegmentItem(menu)
                    if not bool(wrapper._showMasters):
                        menu.addItem_(target._makeActiveOnlyItem())
                    menu.addItem_(NSMenuItem.separatorItem())
            except Exception:
                pass
            try:
                if bool(wrapper._waterfall) and target._narrowHosted():
                    cu = menu.addItemWithTitle_action_keyEquivalent_(
                        "Waterfall Sizes…", "waterfallCustom:", ""
                    )
                    cu.setTarget_(target)
                    menu.addItem_(NSMenuItem.separatorItem())
            except Exception:
                pass
            # View options here only on narrow hosts (floating has a toolbar button);
            # view-hosted checkboxes so the menu survives several toggles.
            try:
                if target._narrowHosted():
                    target._addViewOptionItems(menu)
                    menu.addItem_(NSMenuItem.separatorItem())
            except Exception:
                pass
            try:
                if target._addSortPriorityItems(menu):
                    menu.addItem_(NSMenuItem.separatorItem())
            except Exception:
                pass
            return menu
        except Exception:
            return None

    def acceptsFirstResponder(self):
        return True  # receive arrow keys for instance stepping (keyDown_)

    def keyDown_(self, event):
        # arrows step the Waterfall instance picker; unhandled keys go to super
        handled = False
        try:
            plug = getattr(getattr(self, "wrapper", None), "_plugin", None)
            chars = event.charactersIgnoringModifiers() or ""
            cp = ord(chars[0]) if chars else 0
            if plug is not None and cp in (0xF702, 0xF700):  # left / up  -> previous
                handled = bool(
                    plug.stepFocusItem(-1)
                )  # swallow the key only if it stepped (no-op in Overview)
            elif plug is not None and cp in (0xF703, 0xF701):  # right / down -> next
                handled = bool(plug.stepFocusItem(1))
        except Exception:
            pass
        if not handled:
            try:
                objc.super(AZSuperPreviewView, self).keyDown_(event)
            except Exception:
                pass

    def setFrameSize_(self, newSize):
        objc.super(AZSuperPreviewView, self).setFrameSize_(newSize)
        # Auto columns (Column = 0) make content height depend on width: recompute it.
        # Guarded: updateContentSize re-enters setFrameSize_.
        if getattr(self, "_inAutoResize", False):
            return
        wrapper = getattr(self, "wrapper", None)
        if wrapper is None or int(wrapper._columns) != 0:
            return
        self._inAutoResize = True
        try:
            wrapper.updateContentSize()
        except Exception:
            print(traceback.format_exc())
        finally:
            self._inAutoResize = False

    @objc.python_method
    def glyphForName(self, name, font):
        """Token (glyph name or one character) → GSGlyph. None for whitespace with no
        glyph, so a typed space stays blank instead of a .notdef box."""
        glyph = font.glyphs[name]
        if glyph is None and len(name) == 1:
            if name.isspace():
                glyph = font.glyphs["space"] or font.glyphForUnicode_(
                    f"{ord(name):04X}"
                )
            else:
                glyph = font.glyphForUnicode_(f"{ord(name):04X}")
        if glyph is None and (not name or name.isspace()):
            return None
        if glyph is None:
            glyph = font.glyphs[".notdef"]
        return glyph

    @objc.python_method
    def glyphRTLVote(self, glyph):
        """+1 RTL, -1 LTR, 0 neutral. Prefers GSGlyph.script (covers /glyph tokens,
        which carry no character), else the Unicode range."""
        rtlScripts = (
            "arabic",
            "hebrew",
            "syriac",
            "thaana",
            "nko",
            "adlam",
            "mandaic",
            "samaritan",
        )
        try:
            script = glyph.script
        except Exception:
            script = None
        if script:
            s = str(script).lower()
            if s in rtlScripts:
                return 1
            if s in ("common", "inherited"):
                return 0
            return -1
        # glyph.unicodeChar returns an ObjC selector, not a value: use the hex .unicode
        cp = None
        try:
            u = glyph.unicode
            if u:
                cp = int(u, 16)
        except Exception:
            pass
        if not isinstance(cp, int):
            return 0
        if (
            0x0590 <= cp <= 0x05FF
            or 0x0600 <= cp <= 0x06FF
            or 0x0750 <= cp <= 0x077F
            or 0x08A0 <= cp <= 0x08FF
            or 0xFB1D <= cp <= 0xFDFF
            or 0xFE70 <= cp <= 0xFEFF
        ):
            return 1
        return 0

    @objc.python_method
    def runDirectionIsRTL(self, font, glyphNames):
        """Run direction by majority vote (tie or empty → LTR; no shaping). Memoized on
        (font, sample): the vote costs ~0.5 ms per frame."""
        key = (objectKey(font), tuple(glyphNames or []))
        if self._rtlKey == key:
            return self._rtlVal
        rtl = ltr = 0
        for name in glyphNames:
            glyph = self.glyphForName(name, font)
            if glyph is None:
                continue
            vote = self.glyphRTLVote(glyph)
            if vote > 0:
                rtl += 1
            elif vote < 0:
                ltr += 1
        self._rtlKey = key
        self._rtlVal = rtl > ltr
        return self._rtlVal

    @objc.python_method
    def rowMetrics(self, masterFont, size):
        """Layout metrics from the design font, so every row keeps the same pitch."""
        upm = float(masterFont.upm) or 1000.0
        scale = size / upm
        baseMaster = masterFont.masters[0]
        ascender = baseMaster.ascender if baseMaster.ascender else upm * 0.8
        descender = baseMaster.descender if baseMaster.descender else -upm * 0.2
        # metrics only, not ink: an accented sample must not re-flow every card
        ascUnits, descUnits = float(ascender), float(descender)
        asc = ascUnits * scale + ROW_INK_PAD
        desc = (-descUnits) * scale + ROW_INK_PAD
        labelH = 24.0  # label line + air above the glyphs
        gap = ROW_GAP
        # room below the descender for the spacing value / glyph name lines
        spacingExtra = (
            (SPACING_LINE_H + SPACING_DESC_GAP_UNITS * scale)
            if self.wrapper._showSpacing
            else 0
        )
        rowPitch = labelH + asc + desc + spacingExtra + gap
        topMargin = 14.0
        leftMargin = 14.0
        return scale, asc, desc, labelH, rowPitch, topMargin, leftMargin, spacingExtra

    @objc.python_method
    def rowsForCount(self, count, columns):
        columns = max(1, int(columns))
        if count <= 0:
            return 0
        return (count + columns - 1) // columns

    @objc.python_method
    def contentHeight(self, masterFont, count, size, columns):
        if not masterFont or count <= 0:
            return 60.0
        rowPitch, topMargin = self.rowMetrics(masterFont, size)[4:6]
        return topMargin * 2 + rowPitch * self.rowsForCount(count, columns)

    @objc.python_method
    def waterfallContentHeight(self, masterFont, size):
        """Pixel height of the size ladder at true pt sizes; mirrors drawWaterfall."""
        if not masterFont or size <= 0:
            return 60.0
        asc, desc = self.rowMetrics(masterFont, size)[1:3]
        return self.waterfallRows(asc, desc, float(size))[1]

    @objc.python_method
    def waterfallRows(self, asc, desc, baseSize):
        """waterfallLayout of the ladder split at the visible height, plus the column
        count for the visible width."""
        extents = []
        for s in self.wrapper._waterfallLadder:
            r = s / baseSize
            extents.append(asc * r + desc * r)
        sv = self.enclosingScrollView()
        visible = sv.contentSize() if sv is not None else self.bounds().size
        columns = self.wrapper.effectiveWaterfallColumns(visible.width)
        return waterfallLayout(extents, visible.height, columns) + (columns,)

    @objc.python_method
    def runAdvance(self, font, master, glyphNames):
        """Sample-run advance (font units) for one master, without building outlines.
        LTR kerning suffices: the total width is the same either direction."""
        advance = 0.0
        names = glyphNames or []
        for pos, name in enumerate(names):
            glyph = self.glyphForName(name, font)
            if glyph is None:
                continue
            layer = glyph.layers[master.id]
            if layer is None:
                continue
            kernValue = 0
            if pos + 1 < len(names):
                nextGlyph = self.glyphForName(names[pos + 1], font)
                if nextGlyph is not None:
                    nextLayer = nextGlyph.layers[master.id]
                    if nextLayer is not None:
                        kernValue = getKernValue(layer, nextLayer, False)
            advance += layer.width + kernValue
        return advance

    @objc.python_method
    def _maxRunUnits(self, masterFont, glyphNames):
        """Widest sample-run advance across masters (bounds instance widths).
        Memoized on (font, sample); clearCache drops it when outlines change."""
        key = (objectKey(masterFont), tuple(glyphNames or []))
        if getattr(self, "_maxRunKey", None) == key:
            return self._maxRunVal
        runUnits = 0.0
        try:
            for master in masterFont.masters:
                runUnits = max(
                    runUnits, self.runAdvance(masterFont, master, glyphNames)
                )
        except Exception:
            pass
        self._maxRunKey = key
        self._maxRunVal = runUnits
        return runUnits

    @objc.python_method
    def autoColumnCount(self, masterFont, glyphNames, size, viewWidth, itemCount):
        """Auto (Column = 0) count: as many columns as fit while each holds the widest
        master run unclipped; capped at the item count."""
        if masterFont is None or not masterFont.masters:
            return 1
        metrics = self.rowMetrics(masterFont, size)
        scale, leftMargin = metrics[0], metrics[6]
        runUnits = self._maxRunUnits(masterFont, glyphNames)
        runPts = runUnits * scale
        colGap = ROW_GAP
        # floored so empty or 1-glyph samples don't explode the count
        minCellW = max(runPts + GRAB_HANDLE_W + 6.0, 60.0)
        avail = viewWidth - 2 * leftMargin
        if avail <= 0:
            return 1
        cols = int((avail + colGap) // (minCellW + colGap))
        cols = max(1, cols)
        if itemCount > 0:
            cols = min(cols, itemCount)
        return cols

    @objc.python_method
    def effectiveColumns(self, masterFont, glyphNames, size, viewWidth, itemCount):
        """Manual 1-10 columns, or the Auto count for 0. The G4 sidebar tab is always
        one column; the stored value is floating-only."""
        if bool(self.wrapper._embedded):
            return 1
        raw = int(self.wrapper._columns)
        if raw >= 1:
            return max(1, min(10, raw))
        return self.autoColumnCount(masterFont, glyphNames, size, viewWidth, itemCount)

    @objc.python_method
    def interpolatedFontFor(self, instance):
        """Cached whole-font interpolation, the proxy's fallback. A failure returns
        None uncached, so a transient error self-heals on the next redraw."""
        cache = self.wrapper.instances
        # per instance, so width families whose styles share a name ("Light") don't
        # share one cached interpolation
        key = instanceIdentity(instance)
        font = cache.get(key)
        if font is None:
            try:
                font = instance.interpolatedFont
            except Exception:
                font = None
            if font is not None:
                cache[key] = font
        return font

    @objc.python_method
    def proxyFontFor(self, instance, proxyByInstance):
        """interpolatedFontProxy re-interpolates on every access, so one proxy per
        instance is reused within a draw pass via proxyByInstance."""
        key = id(instance)
        font = proxyByInstance.get(key)
        if font is None:
            try:
                font = instance.interpolatedFontProxy
            except Exception:
                font = None
            proxyByInstance[key] = font
        return font

    @objc.python_method
    def composeLabel(self, name, family, values, masterFont):
        """Family residue + name, plus 'tag=val, …' coords when enabled."""
        family = familyResidue(family, masterFont)
        fullName = (f"{family} {name}").strip() if family else (name or "")
        if not self.wrapper._showCoords:
            return fullName
        axes = masterFont.axes
        if values and axes and len(values) == len(axes):
            coords = ", ".join(
                f"{a.axisTag}={fmtNumber(v)}" for a, v in zip(axes, values)
            )
            return f"{fullName} ({coords})"
        return fullName

    @objc.python_method
    def labelForInstance(self, instance, masterFont):
        try:
            family = instance.familyName or ""
        except Exception:
            family = ""
        return self.composeLabel(
            instance.name or "Instance", family, itemAxes(instance), masterFont
        )

    @objc.python_method
    def labelForMaster(self, master, masterFont):
        return self.composeLabel(
            master.name or "Master", "", itemAxes(master), masterFont
        )

    @objc.python_method
    def itemLabel(self, item, masterFont, showMasters):
        if showMasters:
            return self.labelForMaster(item, masterFont)
        return self.labelForInstance(item, masterFont)

    @objc.python_method
    def _flattenOver(self, colour, back):
        """`colour` over opaque `back` in the window's colour space, made opaque: same
        pixels as the alpha fill, ~10x cheaper. Any doubt returns `colour` unchanged."""
        try:
            alpha = float(colour.alphaComponent())
            if alpha >= 1.0:
                return colour
            window = self.window()
            space = window.colorSpace() if window is not None else None
            if space is None:
                space = NSColorSpace.sRGBColorSpace()
            fg = colour.colorUsingColorSpace_(space)
            bg = back.colorUsingColorSpace_(space)
            if fg is None or bg is None or bg.alphaComponent() < 1.0:
                return colour
            if fg.numberOfComponents() != 4 or bg.numberOfComponents() != 4:
                return colour
            mixed = [
                bg.components()[i] * (1.0 - alpha) + fg.components()[i] * alpha
                for i in range(3)
            ] + [1.0]
            return NSColor.colorWithColorSpace_components_count_(space, mixed, 4)
        except Exception:
            return colour

    @objc.python_method
    def _rowSlantMemo(self, key, item, masterFont, showMasters):
        """_rowSlant memoized per instance row, reset with the path cache."""
        if showMasters:
            return self._rowSlant(item, masterFont, showMasters)
        cache = self._slantCache
        if cache is None:
            cache = self._slantCache = {}
        value = cache.get(key)
        if value is None:
            value = cache[key] = self._rowSlant(item, masterFont, showMasters)
        return value

    @objc.python_method
    def _rowLabelMemo(self, key, item, masterFont, showMasters):
        """itemLabel memoized like _rowSlantMemo; `key` holds name and coords."""
        if showMasters:
            return self.itemLabel(item, masterFont, showMasters)
        cache = self._labelCache
        if cache is None:
            cache = self._labelCache = {}
        memoKey = (key, bool(self.wrapper._showCoords))
        value = cache.get(memoKey)
        if value is None:
            value = cache[memoKey] = self.itemLabel(item, masterFont, showMasters)
        return value

    @objc.python_method
    def resetPathCaches(self):
        """Drop every cache derived from the sample string or the font."""
        self._pathCache = LRUDict(512)
        self._pathSig = object()  # sentinel: forces first-pass fill
        self._displayKey = None
        self._displayItems = None
        self._orderMemo = None
        self._slantCache = None
        self._labelCache = None
        self._rtlKey = None

    @objc.python_method
    def drawFontAndMaster(self, item, masterFont, showMasters, proxyByInstance):
        """Instances use the per-glyph proxy, which interpolates only drawn glyphs;
        whole-font interpolation is the fallback."""
        if showMasters:
            return masterFont, item
        font = self.proxyFontFor(item, proxyByInstance)
        if font is None:
            font = self.interpolatedFontFor(item)
        if font is None:
            return None, None
        return font, font.masters[0]

    @objc.python_method
    def sortInstances(self, items, masterFont, axisOrderTags):
        """Sort by axis values, axisOrderTags first; unlisted axes follow."""
        tagToIndex = {a.axisTag: i for i, a in enumerate(masterFont.axes)}
        order = [tagToIndex[t] for t in (axisOrderTags or []) if t in tagToIndex]
        for i in range(len(masterFont.axes)):
            if i not in order:
                order.append(i)

        def key(item):
            vals = itemAxes(item)
            out = []
            for idx in order:
                v = vals[idx] if idx < len(vals) else None
                try:
                    out.append(float(v))
                except (TypeError, ValueError):
                    out.append(0.0)
            return tuple(out)

        return sorted(items, key=key)

    @objc.python_method
    def applyCustomOrder(self, items, orderStrs, showMasters):
        """Items missing from orderStrs keep their relative order at the end."""
        pos = {s: i for i, s in enumerate(orderStrs)}
        n = len(orderStrs)
        return sorted(items, key=lambda it: pos.get(itemKey(it, showMasters), n))

    @objc.python_method
    def buildStringPath(
        self, font, master, glyphNames, rtl=False, collect=(True, True, True, True)
    ):
        """Kerned font-unit outline of the sample string (baseline y=0, origin x=0) plus
        overlay data. RTL lays glyphs in visual order and swaps kern pair operands."""
        # collect = (sidebearings, anchors, nodes, kern): skip data for hidden overlays
        collectSb, collectAnchors, collectNodes, collectKern = collect
        fullPath = NSBezierPath.alloc().init()
        advance = 0.0
        kernBars = []
        sbMetrics = []
        anchorPts = []
        nodePts = []
        handleSegs = []
        order = list(range(len(glyphNames)))
        if rtl:
            order = order[::-1]
        # resolve each layer once: a cold proxy re-interpolates on every fetch
        resolved = []
        for idx in order:
            glyph = self.glyphForName(glyphNames[idx], font)
            if glyph is None:
                continue
            layer = glyph.layers[master.id]
            if layer is None:
                continue
            resolved.append(layer)
        for i, layer in enumerate(resolved):
            layerPath = layer.completeBezierPath
            if layerPath is None:
                layerPath = NSBezierPath.alloc().init()
            t = NSAffineTransform.transform()
            t.translateXBy_yBy_(advance, 0)
            layerPath.transformUsingAffineTransform_(t)
            fullPath.appendBezierPath_(layerPath)

            if collectSb:
                try:
                    lsb, rsb = layer.LSB, layer.RSB
                except Exception:
                    lsb = rsb = None
                sbMetrics.append((advance, layer.width, lsb, rsb))

            try:
                for anchor in (
                    layer.anchors.values() if collectAnchors else ()
                ):  # G4 DictProxy: .values() gives the anchor objects
                    apos = anchor.position
                    anchorPts.append((advance + apos.x, apos.y))
            except Exception:
                pass

            # only the layer's own paths carry nodes: composites show none, as in Glyphs
            try:
                for path in layer.paths if collectNodes else ():
                    # read each node once: indexing the proxy per handle is ~3x slower
                    nodes = list(path.nodes)
                    count = len(nodes)
                    positions = [node.position for node in nodes]
                    types = [node.type for node in nodes]
                    for ni, node in enumerate(nodes):
                        npos = positions[ni]
                        isOff = types[ni] == OFFCURVE
                        if isOff:
                            kind = 2
                        elif getattr(node, "smooth", False):
                            kind = 1
                        else:
                            kind = 0
                        nodePts.append((advance + npos.x, npos.y, kind))
                        if isOff:
                            for adj in (ni - 1, (ni + 1) % count):
                                if types[adj] != OFFCURVE:
                                    apos = positions[adj]
                                    handleSegs.append(
                                        (
                                            advance + npos.x,
                                            npos.y,
                                            advance + apos.x,
                                            apos.y,
                                        )
                                    )
            except Exception:
                pass

            kernValue = 0
            if i + 1 < len(resolved):
                nextLayer = resolved[i + 1]
                if rtl:
                    kernValue = getKernValue(nextLayer, layer, True)
                else:
                    kernValue = getKernValue(layer, nextLayer, False)
            if kernValue and collectKern:
                kernBars.append(kernBarSpan(advance + layer.width, kernValue))
            advance += layer.width + kernValue
        # this row's own metrics, so guides land on its glyphs, not the base master's
        metricVals = [0.0]
        for attr in ("ascender", "capHeight", "xHeight", "descender"):
            try:
                v = getattr(master, attr)
                if v is None:
                    continue
                fv = float(v)
                if not any(abs(fv - existing) < 0.5 for existing in metricVals):
                    metricVals.append(fv)
            except Exception:
                pass
        return (
            fullPath,
            advance,
            kernBars,
            sbMetrics,
            anchorPts,
            (nodePts, handleSegs),
            metricVals,
        )

    @objc.python_method
    def instancePathFor(
        self,
        rowToken,
        item,
        masterFont,
        glyphNames,
        proxyByInstance,
        rtl=False,
        collect=(True, True, True, True),
        showMasters=False,
    ):
        """Cached font-unit outline + overlay data, path copied (callers transform it in
        place); None if it can't be built. The cache busts with wrapper._liveSig."""
        sig = self.wrapper._liveSig
        if self._pathSig != sig:
            self._pathCache = LRUDict(512)
            self._pathSig = sig
        # an overlay toggle re-lays out from the warm interpolation cache
        key = (rowToken, (tuple(glyphNames), bool(rtl), tuple(collect)))
        entry = self._pathCache.get(key)
        if entry is None:
            font, master = self.drawFontAndMaster(
                item, masterFont, showMasters, proxyByInstance
            )
            if font is None or master is None:
                return None
            entry = self._pathCache[key] = self.buildStringPath(
                font, master, glyphNames, rtl, collect
            )
        return (entry[0].copy(),) + entry[1:]

    @objc.python_method
    def drawGhostPath(self, fontUnitPath, cellX, baselineY, scale, colour, xOffset=0.0):
        """Fill a copy of a font-unit path at (cellX + xOffset, baselineY)."""
        p = fontUnitPath.copy()
        t = NSAffineTransform.transform()
        t.translateXBy_yBy_(cellX + xOffset, baselineY)
        t.scaleXBy_yBy_(scale, -scale)
        p.transformUsingAffineTransform_(t)
        colour.set()
        p.fill()

    @objc.python_method
    def drawRowGuides(self, cellX, colW, baselineY, scale, metrics, fore):
        """Faint rules at the row's metrics (font-unit offsets from the baseline)."""
        fore.colorWithAlphaComponent_(0.30).set()
        drawnY = []  # pixel dedup: coincident metrics draw once
        for mv in metrics:
            y = baselineY - mv * scale
            if any(abs(y - dy) < 1.0 for dy in drawnY):
                continue
            drawnY.append(y)
            line = NSBezierPath.bezierPath()
            line.moveToPoint_((cellX, y))
            line.lineToPoint_((cellX + colW, y))
            line.setLineWidth_(0.5)
            line.stroke()

    @objc.python_method
    def drawKernBars(
        self,
        bars,
        cellX,
        xOffset,
        baselineY,
        scale,
        ascUnits,
        descUnits,
        colours,
        slantTan=0.0,
        pivotUnits=0.0,
    ):
        """Kern bars in (positive, negative) `colours`, drawn over the ink so a negative
        kern's overlap reads as a tint. Italic rows shear them around pivotUnits."""
        top = baselineY - ascUnits * scale
        height = (ascUnits - descUnits) * scale
        if height <= 0:
            return
        topShift = slantTan * (ascUnits - pivotUnits) * scale
        botShift = slantTan * (descUnits - pivotUnits) * scale
        for x0, width, positive in bars:
            if width <= 0:
                continue
            (colours[0] if positive else colours[1]).set()
            barX = cellX + xOffset + x0 * scale
            barW = width * scale
            if slantTan:
                bar = NSBezierPath.bezierPath()
                bar.moveToPoint_((barX + topShift, top))
                bar.lineToPoint_((barX + barW + topShift, top))
                bar.lineToPoint_((barX + barW + botShift, top + height))
                bar.lineToPoint_((barX + botShift, top + height))
                bar.closePath()
                bar.fill()
            else:
                NSBezierPath.fillRect_(NSMakeRect(barX, top, barW, height))

    @objc.python_method
    def _rowSlant(self, item, masterFont, showMasters):
        """(tan(slant), xHeight/2): the shear Glyphs gives metric lines in an italic.
        Angle from the blended italicAngle, else -slnt axis value (slnt is CCW+)."""
        angle = xheight = 0.0
        try:
            if showMasters:
                try:
                    angle = float(item.italicAngle or 0.0)
                except Exception:
                    angle = 0.0
                try:
                    xheight = float(item.xHeight or 0.0)
                except Exception:
                    xheight = 0.0
            else:
                coeffs = self._mixCoeffs(item)
                byId = {m.id: m for m in masterFont.masters}
                wsum = 0.0
                for mid, weight in (coeffs or {}).items():
                    master = byId.get(mid)
                    if master is None:
                        continue
                    angle += float(master.italicAngle or 0.0) * weight
                    xheight += float(master.xHeight or 0.0) * weight
                    wsum += weight
                if wsum > 1e-9:
                    angle /= wsum
                    xheight /= wsum
                else:
                    angle = xheight = 0.0
        except Exception:
            angle = 0.0
        if abs(angle) < 0.01:
            try:
                for axIdx, axis in enumerate(masterFont.axes or []):
                    if str(axis.axisTag) == "slnt":
                        coords = itemAxes(item)
                        if axIdx < len(coords):
                            angle = -float(coords[axIdx] or 0.0)
                        break
            except Exception:
                angle = 0.0
        if abs(angle) < 0.01:
            return 0.0, 0.0
        if xheight <= 0.0:
            try:
                xheight = float(masterFont.masters[0].xHeight or 0.0)
            except Exception:
                xheight = 0.0
        return math.tan(math.radians(angle)), xheight / 2.0

    @objc.python_method
    def drawSpacing(
        self,
        metrics,
        cellX,
        xOffset,
        baselineY,
        scale,
        ascUnits,
        descUnits,
        ruleColour,
        textColour,
        lineY,
        slantTan=0.0,
        pivotUnits=0.0,
    ):
        """Advance-edge rules plus LSB . width . RSB at `lineY` under each glyph. Italic
        rows shear the rules around pivotUnits so they sit on the slanted edges."""
        top = baselineY - ascUnits * scale
        height = (ascUnits - descUnits) * scale
        attrs = {
            NSFontAttributeName: OVERLAY_VALUE_FONT,
            NSForegroundColorAttributeName: textColour,
        }
        gap = 2.0
        topShift = slantTan * (ascUnits - pivotUnits) * scale
        botShift = slantTan * (descUnits - pivotUnits) * scale
        ruleColour.set()
        for originX, width, lsb, rsb in metrics:
            leftX = cellX + xOffset + originX * scale
            rightX = cellX + xOffset + (originX + width) * scale
            if height > 0:
                for edgeX in (leftX, rightX):
                    rule = NSBezierPath.bezierPath()
                    rule.moveToPoint_((edgeX + topShift, top))
                    rule.lineToPoint_((edgeX + botShift, top + height))
                    rule.setLineWidth_(SB_RULE_WIDTH)
                    rule.stroke()
            # the value line follows the rules' sheared bottom ends
            leftX += botShift
            rightX += botShift
            lsbStr = (
                NSAttributedString.alloc().initWithString_attributes_(
                    str(int(round(lsb))), attrs
                )
                if lsb is not None
                else None
            )
            rsbStr = (
                NSAttributedString.alloc().initWithString_attributes_(
                    str(int(round(rsb))), attrs
                )
                if rsb is not None
                else None
            )
            lsbW = lsbStr.size().width if lsbStr is not None else 0.0
            rsbW = rsbStr.size().width if rsbStr is not None else 0.0
            if lsbStr is not None:
                try:
                    lsbStr.drawAtPoint_((leftX + gap, lineY))
                except Exception:
                    pass
            if rsbStr is not None:
                try:
                    rsbStr.drawAtPoint_((rightX - gap - rsbW, lineY))
                except Exception:
                    pass
            # skip the width when it would collide with LSB/RSB (narrow glyphs like "i")
            wStr = NSAttributedString.alloc().initWithString_attributes_(
                str(int(round(width))), attrs
            )
            wW = wStr.size().width
            centerX = (leftX + rightX) / 2.0
            leftBound = (leftX + gap + lsbW) if lsbStr is not None else leftX
            rightBound = (rightX - gap - rsbW) if rsbStr is not None else rightX
            clearance = 2.0
            if (centerX - wW / 2.0) > leftBound + clearance and (
                centerX + wW / 2.0
            ) < rightBound - clearance:
                try:
                    wStr.drawAtPoint_((centerX - wW / 2.0, lineY))
                except Exception:
                    pass

    @objc.python_method
    def _backRGB(self):
        c = self.wrapper._backColour
        rgb = c.colorUsingColorSpaceName_("NSCalibratedRGBColorSpace") or c
        return rgb.redComponent(), rgb.greenComponent(), rgb.blueComponent()

    @objc.python_method
    def _backIsDark(self):
        r, g, b = self._backRGB()
        return 0.299 * r + 0.587 * g + 0.114 * b < 0.5

    @objc.python_method
    def _cardSurfaceColour(self):
        """Opaque card-surface colour for knockout halos; mirrors cardColour's tint."""
        try:
            r, g, b = self._backRGB()
            tint, a = (1.0, 0.08) if self._backIsDark() else (0.0, 0.05)
            r = r * (1 - a) + tint * a
            g = g * (1 - a) + tint * a
            b = b * (1 - a) + tint * a
            return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0)
        except Exception:
            return NSColor.whiteColor()

    @objc.python_method
    def _lozengePath(self, cx, cy, r):
        """Diamond centred at (cx, cy) with half-diagonal r."""
        loz = NSBezierPath.bezierPath()
        loz.moveToPoint_((cx, cy - r))
        loz.lineToPoint_((cx + r, cy))
        loz.lineToPoint_((cx, cy + r))
        loz.lineToPoint_((cx - r, cy))
        loz.closePath()
        return loz

    @objc.python_method
    def drawAnchors(
        self, points, cellX, xOffset, baselineY, scale, colour, markerScale
    ):
        """Anchor lozenges, each over a knockout halo that erases ink so it reads crisp
        without a stroke."""
        radius = 2.4 * markerScale
        haloPad = 1.0 * markerScale
        placed = [
            (cellX + xOffset + fx * scale, baselineY - fy * scale) for fx, fy in points
        ]
        self._cardSurfaceColour().set()
        for cx, cy in placed:
            self._lozengePath(cx, cy, radius + haloPad).fill()
        colour.set()
        for cx, cy in placed:
            self._lozengePath(cx, cy, radius).fill()

    @objc.python_method
    def drawNodes(
        self,
        nodeData,
        cellX,
        xOffset,
        baselineY,
        scale,
        cornerCol,
        smoothCol,
        offCol,
        handleCol,
        markerScale,
    ):
        """Nodes as Glyphs draws them (kind 0 corner square, 1 smooth disc, 2 off-curve
        disc) plus handles, each marker over a knockout halo."""
        points, handles = nodeData
        cornerHalf = 1.7 * markerScale
        smoothRad = 1.9 * markerScale
        offRad = 1.5 * markerScale
        haloPad = 1.0 * markerScale
        # one path per colour, filled once: per-node fills cost a bridge call + CG fill
        # each. Same-shape subpaths share winding, so overlaps union under nonzero fill.
        ox = cellX + xOffset
        if handles:
            seg = NSBezierPath.bezierPath()
            for x0, y0, x1, y1 in handles:
                seg.moveToPoint_((ox + x0 * scale, baselineY - y0 * scale))
                seg.lineToPoint_((ox + x1 * scale, baselineY - y1 * scale))
            seg.setLineWidth_(SB_RULE_WIDTH * markerScale)
            handleCol.set()
            seg.stroke()
        haloSquares = NSBezierPath.bezierPath()
        haloDiscs = NSBezierPath.bezierPath()
        marks = (
            NSBezierPath.bezierPath(),
            NSBezierPath.bezierPath(),
            NSBezierPath.bezierPath(),
        )
        for fx, fy, kind in points:
            cx = ox + fx * scale
            cy = baselineY - fy * scale
            if kind == 0:
                h = cornerHalf + haloPad
                haloSquares.appendBezierPathWithRect_(
                    NSMakeRect(cx - h, cy - h, h * 2, h * 2)
                )
                marks[0].appendBezierPathWithRect_(
                    NSMakeRect(
                        cx - cornerHalf, cy - cornerHalf, cornerHalf * 2, cornerHalf * 2
                    )
                )
            else:
                r = smoothRad if kind == 1 else offRad
                halo = r + haloPad
                haloDiscs.appendBezierPathWithOvalInRect_(
                    NSMakeRect(cx - halo, cy - halo, halo * 2, halo * 2)
                )
                marks[kind].appendBezierPathWithOvalInRect_(
                    NSMakeRect(cx - r, cy - r, r * 2, r * 2)
                )
        self._cardSurfaceColour().set()
        haloSquares.fill()
        haloDiscs.fill()
        offCol.set()
        marks[2].fill()
        smoothCol.set()
        marks[1].fill()
        cornerCol.set()
        marks[0].fill()

    @objc.python_method
    def drawAlignmentZones(self, cellX, colW, baselineY, scale, zones, colour):
        """Full-width overshoot bands; `zones` are font-unit (position, signed size)."""
        colour.set()
        for position, size in zones:
            y0 = baselineY - position * scale
            y1 = baselineY - (position + size) * scale
            top = min(y0, y1)
            height = abs(y1 - y0)
            if height <= 0:
                continue
            NSBezierPath.fillRect_(NSMakeRect(cellX, top, colW, height))

    @objc.python_method
    def _glyphsColor(self, key, fallback):
        """Glyphs' Edit-view colour for `key` from user defaults (key + "Dark" on dark
        backgrounds), else the (r, g, b, a) `fallback`."""
        try:
            realKey = key + "Dark" if self._backIsDark() else key
            data = Glyphs.defaults[realKey]
            if data is None:
                data = Glyphs.defaults[key]
            if data is not None:
                from Foundation import NSUnarchiver, NSKeyedUnarchiver

                try:  # Glyphs stores these with NSArchiver; keyed archive as fallback
                    col = NSUnarchiver.unarchiveObjectWithData_(data)
                except Exception:
                    col = None
                if col is None:
                    try:
                        col = NSKeyedUnarchiver.unarchiveObjectWithData_(data)
                    except Exception:
                        col = None
                if col is not None:
                    return col
        except Exception:
            pass
        r, g, b, a = fallback
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)

    @objc.python_method
    def cardColour(self):
        """Faint row-card tint: 5% black on light backgrounds, 8% white on dark ones."""
        try:
            dark = self._backIsDark()
        except Exception:
            dark = False
        if dark:
            return NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.08)
        return NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.05)

    @objc.python_method
    def dragCardColour(self):
        return NSColor.keyboardFocusIndicatorColor()

    @objc.python_method
    def drawRowCard(self, cellX, rowTop, colW, contentH, colour):
        pad = ROW_CARD_PAD
        rect = NSMakeRect(
            cellX - pad, rowTop - pad, colW + 2 * pad, max(1.0, contentH + 2 * pad)
        )
        colour.set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 6.0, 6.0).fill()

    @objc.python_method
    def drawGrabHandle(self, x, centerY, fore):
        """Faint 2x3 dot drag grip, vertically centred on centerY."""
        fore.colorWithAlphaComponent_(0.30).set()
        dot, gapX, gapY = 1.7, 4.0, 3.5
        yTop = centerY - (dot + 2 * gapY) / 2.0
        for cx in range(2):
            for cy in range(3):
                NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(x + cx * gapX, yTop + cy * gapY, dot, dot)
                ).fill()

    @objc.python_method
    def drawDropIndicator(self):
        """Insertion line at the top of the landing cell, or below the last cell for an
        append, so a drop at the very end previews right under column-major reflow."""
        src = self._dragIndex
        dst = self._dropIndex
        info = self._gridLayout
        if src is None or dst is None or not info:
            return
        keys, _, numRows, colW, colGap, rowPitch, topMargin, leftMargin = info
        n = len(keys)
        if numRows <= 0 or n <= 0:
            return
        final, append = dropIndicatorCell(int(src), int(dst), n)
        col, row = gridColRow(final, numRows)
        x = gridCellX(col, colW, colGap, leftMargin)
        yy = topMargin + (row + (1 if append else 0)) * rowPitch
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.12, 0.45, 1.0, 0.95).set()
        line = NSBezierPath.bezierPath()
        line.moveToPoint_((x, yy))
        line.lineToPoint_((x + colW, yy))
        line.setLineWidth_(3.0)
        line.stroke()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x - 4, yy - 4, 8, 8)).fill()

    def drawRect_(self, rect):
        # clicks and drags hit-test this; only a frame that draws the grid sets it
        self._gridLayout = None
        try:
            wrapper = self.wrapper
            back = wrapper.effectiveBackColour()  # Waterfall -> house card grey
            fore = wrapper.effectiveForeColour()
            back.set()
            NSBezierPath.fillRect_(self.bounds())

            _plugin = wrapper._plugin
            # Glyphs.font costs ~0.6 ms (sync XPC): use the plugin's TTL cache
            masterFont = _plugin._cur_font()
            if masterFont is None:
                self.drawLabel("Open a font to preview its instances.", fore, 14, 14)
                return

            glyphNames = wrapper._glyphsList

            showMasters = wrapper.effectiveShowMasters()  # Waterfall forces instances
            # items memoized: the staticInstances filter costs ~1.3 ms/frame
            activeOnly = wrapper.effectiveActiveOnly()  # Waterfall shows ALL instances
            _dk = (objectKey(masterFont), bool(showMasters), bool(activeOnly))
            if self._displayKey == _dk:
                items = self._displayItems
            else:
                items = displayItems(masterFont, showMasters, activeOnly)
                self._displayKey = _dk
                self._displayItems = items
                self._orderMemo = None
            if (
                not items and not wrapper._waterfall
            ):  # Waterfall falls back to the master
                if showMasters:
                    msg = "This font has no masters."
                elif wrapper._activeOnly and staticInstances(masterFont):
                    msg = "No active instances — uncheck “Active instances only” to show all."
                else:
                    msg = "This font has no static instances."
                self.drawLabel(msg, fore, 14, 14)
                return
            if not glyphNames:
                self.drawLabel("Type some text above.", fore, 14, 14)
                return

            runIsRTL = self.runDirectionIsRTL(masterFont, glyphNames)
            clipMinY = rect.origin.y
            clipMaxY = rect.origin.y + rect.size.height

            # sort + row keys read every item over the bridge (~70 ms at 69 instances)
            # the TTL is the invalidation: Font Info edits show once it expires
            orderKey = (
                _dk,
                tuple(wrapper._customOrder or ()),
                tuple(wrapper._axisOrder or ()),
            )
            now = time.monotonic()
            memo = self._orderMemo
            if (
                memo is not None
                and memo[0] == orderKey
                and now - memo[1] < ORDER_MEMO_TTL
            ):
                items, itemKeys = memo[2], memo[3]
            else:
                if wrapper._customOrder:
                    items = self.applyCustomOrder(
                        items, wrapper._customOrder, showMasters
                    )
                else:
                    items = self.sortInstances(items, masterFont, wrapper._axisOrder)
                itemKeys = [itemKey(it, showMasters) for it in items]
                self._orderMemo = (orderKey, now, items, itemKeys)

            viewWidth = self.bounds().size.width
            proxyByInstance = {}
            collectTuple = (
                bool(wrapper._showSpacing),
                bool(wrapper._showAnchors and wrapper._showOutlines),
                bool(wrapper._showNodes and wrapper._showOutlines),
                bool(wrapper._showKerning),
            )
            size = float(wrapper._size)
            markerScale = min(4.0, max(0.5, size / MARKER_REF_SIZE))
            columns = self.effectiveColumns(
                masterFont, glyphNames, size, viewWidth, len(items)
            )
            scale, asc, desc, labelH, rowPitch, topMargin, leftMargin, spacingExtra = (
                self.rowMetrics(masterFont, size)
            )

            # column-major, so reading down a column follows the family order
            numRows = self.rowsForCount(len(items), columns)
            colGap = ROW_GAP
            colW = max(
                1.0, (viewWidth - 2 * leftMargin - (columns - 1) * colGap) / columns
            )
            ctx = NSGraphicsContext.currentContext()

            # overlay colours stay None when off, which skips their draw
            kernCol = None
            if wrapper._showKerning and not wrapper._waterfall:  # Overview only
                dark = bool(getattr(wrapper._plugin, "_darkMode", False))
                kernCol = (glyphsKernColour(True, dark), glyphsKernColour(False, dark))

            # Waterfall: one instance at an ascending size ramp instead of the grid
            if wrapper._waterfall:
                try:
                    self._drawWaterfallMode(
                        items,
                        itemKeys,
                        masterFont,
                        glyphNames,
                        proxyByInstance,
                        runIsRTL,
                        collectTuple,
                        scale,
                        asc,
                        desc,
                        size,
                        leftMargin,
                        fore,
                    )
                except Exception:
                    pass
                self._endFrame()
                return

            self._gridLayout = (
                itemKeys,
                columns,
                numRows,
                colW,
                colGap,
                rowPitch,
                topMargin,
                leftMargin,
            )
            self._labelH = labelH  # drag pick-up region

            dragging = self._dragIndex is not None and self._dropIndex is not None

            zoneBands = None
            zoneCol = None
            if wrapper._showZones and wrapper._showGuides:
                zoneBands = []
                try:
                    for zone in masterFont.masters[0].alignmentZones:
                        zoneBands.append((float(zone.position), float(zone.size)))
                except Exception:
                    zoneBands = []
                zoneCol = self._glyphsColor("GSColorZones", (0.76, 0.59, 0.41, 0.20))

            # locked reference row (shift-click) is ghosted behind every other row
            lockedKey = wrapper._lockedKey
            lockedPath = None
            if lockedKey:
                for it, itKey in zip(items, itemKeys):
                    if itKey == lockedKey:
                        # overlays off: only path + advance are drawn for the ghost
                        lockedPath = self.instancePathFor(
                            itKey,
                            it,
                            masterFont,
                            glyphNames,
                            proxyByInstance,
                            runIsRTL,
                            (False, False, False, False),
                            showMasters,
                        )
                        break

            cardCol = self._flattenOver(self.cardColour(), back)
            dragCol = self.dragCardColour()

            sbRuleCol = sbTextCol = None
            if wrapper._showSpacing:
                sbRuleCol = NSColor.systemGrayColor().colorWithAlphaComponent_(0.85)
                sbTextCol = NSColor.systemGrayColor()
            anchorCol = (
                NSColor.systemOrangeColor()
                if (wrapper._showAnchors and wrapper._showOutlines)
                else None
            )
            nodeCornerCol = nodeSmoothCol = nodeOffCol = nodeHandleCol = None
            if wrapper._showNodes and wrapper._showOutlines:
                nodeCornerCol = self._glyphsColor(
                    "GSColorNodeCorner", (0.24, 0.427, 0.75, 1.0)
                )
                nodeSmoothCol = self._glyphsColor(
                    "GSColorNodeSmooth", (0.238, 0.68, 0.40, 1.0)
                )
                nodeOffCol = NSColor.lightGrayColor()
                nodeHandleCol = NSColor.lightGrayColor().colorWithAlphaComponent_(0.8)

            for i, item in enumerate(items):
                col, row = gridColRow(i, numRows)
                cellX = gridCellX(col, colW, colGap, leftMargin)
                rowTop = topMargin + row * rowPitch
                baselineY = rowTop + labelH + asc

                # cull rows outside the dirty rect (flipped view: same y-space)
                rowTopDraw = rowTop - ROW_CARD_PAD
                rowBotDraw = rowTop + labelH + asc + desc + spacingExtra + ROW_CARD_PAD
                if rowBotDraw < clipMinY or rowTopDraw > clipMaxY:
                    continue

                res = self.instancePathFor(
                    itemKeys[i],
                    item,
                    masterFont,
                    glyphNames,
                    proxyByInstance,
                    runIsRTL,
                    collectTuple,
                    showMasters,
                )
                if res is None:
                    continue
                (
                    fullPath,
                    rowAdvance,
                    rowKernBars,
                    rowSbMetrics,
                    rowAnchors,
                    rowNodes,
                    rowMetricVals,
                ) = res

                if runIsRTL:
                    xOffset = max(0.0, colW - rowAdvance * scale)
                else:
                    xOffset = 0.0
                    # nudge ink left of the origin (italic feet) clear of the card clip
                    try:
                        inkMinX = (
                            fullPath.bounds().origin.x
                        )  # font units, pre-transform
                        if inkMinX < 0.0:
                            xOffset = max(xOffset, CARD_INK_LEFT_PAD - inkMinX * scale)
                    except Exception:
                        pass

                dragged = dragging and i == self._dragIndex
                self.drawRowCard(
                    cellX,
                    rowTop,
                    colW,
                    labelH + asc + desc + spacingExtra,
                    dragCol if dragged else cardCol,
                )

                ctx.saveGraphicsState()
                NSBezierPath.bezierPathWithRect_(
                    NSMakeRect(cellX, rowTop, colW, rowPitch)
                ).addClip()

                # 6.5: optical centre of the 11 pt label, so the grip lines up with it
                self.drawGrabHandle(cellX, rowTop + 6.5, fore)
                fore.set()
                self.drawLabel(
                    self._rowLabelMemo(itemKeys[i], item, masterFont, showMasters),
                    fore,
                    cellX + GRAB_HANDLE_W,
                    rowTop,
                    colW - GRAB_HANDLE_W,
                )

                # z-order: zones, guides, ghost, glyph fill, then overlays on top
                if zoneCol is not None and zoneBands:
                    self.drawAlignmentZones(
                        cellX, colW, baselineY, scale, zoneBands, zoneCol
                    )
                if wrapper._showGuides:
                    self.drawRowGuides(
                        cellX, colW, baselineY, scale, rowMetricVals, fore
                    )
                if lockedPath is not None and itemKeys[i] != lockedKey:
                    ghostPath, ghostAdvance, _, _, _, _, _ = lockedPath
                    ghostX = max(0.0, colW - ghostAdvance * scale) if runIsRTL else 0.0
                    self.drawGhostPath(
                        ghostPath,
                        cellX,
                        baselineY,
                        scale,
                        fore.colorWithAlphaComponent_(0.22),
                        ghostX,
                    )

                transform = NSAffineTransform.transform()
                transform.translateXBy_yBy_(cellX + xOffset, baselineY)
                transform.scaleXBy_yBy_(scale, -scale)
                fullPath.transformUsingAffineTransform_(transform)
                glyphColour = (
                    NSColor.systemBlueColor()
                    if (not dragging and i == self._hoverIndex)
                    else fore
                )
                if wrapper._showOutlines:
                    glyphColour.colorWithAlphaComponent_(0.1).set()
                    fullPath.fill()
                    glyphColour.colorWithAlphaComponent_(0.7).set()
                    fullPath.setLineWidth_(SB_RULE_WIDTH)
                    fullPath.stroke()
                else:
                    glyphColour.set()
                    fullPath.fill()

                # kern + spacing overlays span the same per-row band as the guides
                rowBandAsc = max(rowMetricVals)
                rowBandDesc = min(rowMetricVals)

                if (kernCol is not None and rowKernBars) or (
                    sbRuleCol is not None and rowSbMetrics
                ):
                    rowSlantTan, rowSlantPivot = self._rowSlantMemo(
                        itemKeys[i], item, masterFont, showMasters
                    )
                if kernCol is not None and rowKernBars:
                    self.drawKernBars(
                        rowKernBars,
                        cellX,
                        xOffset,
                        baselineY,
                        scale,
                        rowBandAsc,
                        rowBandDesc,
                        kernCol,
                        slantTan=rowSlantTan,
                        pivotUnits=rowSlantPivot,
                    )

                if sbRuleCol is not None and rowSbMetrics:
                    self.drawSpacing(
                        rowSbMetrics,
                        cellX,
                        xOffset,
                        baselineY,
                        scale,
                        rowBandAsc,
                        rowBandDesc,
                        sbRuleCol,
                        sbTextCol,
                        baselineY + (SPACING_DESC_GAP_UNITS - rowBandDesc) * scale,
                        slantTan=rowSlantTan,
                        pivotUnits=rowSlantPivot,
                    )

                if anchorCol is not None and rowAnchors:
                    self.drawAnchors(
                        rowAnchors,
                        cellX,
                        xOffset,
                        baselineY,
                        scale,
                        anchorCol,
                        markerScale,
                    )

                if nodeCornerCol is not None and (rowNodes[0] or rowNodes[1]):
                    self.drawNodes(
                        rowNodes,
                        cellX,
                        xOffset,
                        baselineY,
                        scale,
                        nodeCornerCol,
                        nodeSmoothCol,
                        nodeOffCol,
                        nodeHandleCol,
                        markerScale,
                    )

                ctx.restoreGraphicsState()

            self._endFrame()

        except Exception:
            # read format_exc() here: sys.exc_info() is cleared on block exit
            print("AZ Super Preview draw error:\n", traceback.format_exc())

    @objc.python_method
    def _endFrame(self):
        self.drawDropIndicator()
        plugin = self.wrapper._plugin
        if (
            getattr(plugin, "_phoneServer", None) is not None
            and not plugin._phoneSnapshotting
        ):
            plugin._phoneDirty = True  # repaint = remote page snapshot stale

    @objc.python_method
    def _drawWaterfallMode(
        self,
        items,
        itemKeys,
        masterFont,
        glyphNames,
        proxyByInstance,
        runIsRTL,
        collectTuple,
        scale,
        asc,
        desc,
        size,
        leftMargin,
        fore,
    ):
        """Resolve the picked instance's outline, else the current master's, for
        drawWaterfall. A pinned (shift-clicked) instance doesn't count as picked."""
        showMasters = False  # Waterfall previews instances
        key = self.wrapper._focusKey
        item = items[itemKeys.index(key)] if key and key in itemKeys else None
        if item is None:
            # no instance picked: ramp the master selected in the Edit view
            try:
                item = masterFont.masters[masterFont.masterIndex]
            except Exception:
                item = masterFont.masters[0] if masterFont.masters else None
            if item is None:
                return
            showMasters = True
            key = itemKey(item, showMasters)
        res = self.instancePathFor(
            key,
            item,
            masterFont,
            glyphNames,
            proxyByInstance,
            runIsRTL,
            collectTuple,
            showMasters,
        )
        if not res:
            return
        # res = (path, advance, kernBars, sbMetrics, anchors, nodes, metricVals)
        self.drawWaterfall(
            res[0], res[1], leftMargin, scale, asc, desc, size, fore, runIsRTL
        )

    @objc.python_method
    def drawWaterfall(
        self,
        fullPath,
        rowAdvance,
        x0,
        scale,
        asc,
        desc,
        baseSize,
        fore,
        runIsRTL,
    ):
        """Stack the run at each ladder size at true point size (the scroll view handles
        overflow). No overlays: metrics, kerning and editing marks would pile up at
        every row."""
        ladder = self.wrapper._waterfallLadder
        if baseSize <= 0 or (asc + desc) <= 0:
            return
        rows, _, columns = self.waterfallRows(asc, desc, baseSize)
        multi = columns > 1
        labelW = 28.0
        # no title header: the picker already names the instance
        fullW = self.bounds().size.width
        # labels and separators centre on the metric band (font units, y up), not
        # the run's ink box, so an accented sample can't shift them
        inkTop = (asc - ROW_INK_PAD) / (scale or 1.0)
        inkBottom = -(desc - ROW_INK_PAD) / (scale or 1.0)
        labelCol = fore.colorWithAlphaComponent_(0.45)
        sepCol = fore.colorWithAlphaComponent_(0.15)
        # equal columns with x0 gutters; one column spans the width between margins
        colW = (fullW - (columns + 1) * x0) / columns
        for rowIdx, s in enumerate(ladder):
            col, y = rows[rowIdx]
            cx = x0 + col * (colW + x0)
            # one column: separators run to 8 px from the edge
            sepEnd = cx + colW if multi else fullW - 8.0
            r = s / baseSize
            ln_asc = asc * r
            sc = scale * r
            baseY = y + ln_asc
            # an 11pt label line is ~13px tall
            inkMidPx = baseY - (inkTop + inkBottom) * 0.5 * sc
            self.drawLabel(f"{round(s)}", labelCol, cx, inkMidPx - 7.0)
            glyphX = cx + labelW
            avW = max(1.0, colW - labelW)
            xOff = max(0.0, avW - rowAdvance * sc) if runIsRTL else 0.0
            ctx = NSGraphicsContext.currentContext()
            if multi:  # a long run stops at its column edge
                ctx.saveGraphicsState()
                NSBezierPath.clipRect_(
                    NSMakeRect(cx, 0.0, colW, self.bounds().size.height)
                )
            self.drawGhostPath(fullPath, glyphX, baseY, sc, fore, xOff)
            if multi:
                ctx.restoreGraphicsState()
            # separator midway between this row's bottom and the next row's top
            if rowIdx < len(ladder) - 1 and rows[rowIdx + 1][0] == col:
                rNext = ladder[rowIdx + 1] / baseSize
                yNext = rows[rowIdx + 1][1]
                inkBottomPx = baseY - inkBottom * sc
                nextInkTopPx = (yNext + asc * rNext) - inkTop * (scale * rNext)
                sepY = (inkBottomPx + nextInkTopPx) * 0.5
                sepCol.set()
                sep = NSBezierPath.bezierPath()
                sep.moveToPoint_((cx, sepY))
                sep.lineToPoint_((sepEnd, sepY))
                sep.setLineWidth_(0.5)
                sep.stroke()

    @objc.python_method
    def _mixCoeffs(self, inst):
        """{masterId: weight} for display only; brace-pair (non-scalar) values are
        summed."""
        try:
            weights = inst.instanceInterpolations
            if not weights:
                return None
            out = {}
            for mid in weights:
                v = weights[mid]
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    try:
                        f = sum(float(x) for x in v)
                    except Exception:
                        continue
                out[str(mid)] = f
            return out or None
        except Exception:
            return None

    @objc.python_method
    def drawLabel(self, text, textColour, x, y, maxWidth=None):
        """11 pt label; a trailing ' (axis values)' group is dimmed to half opacity."""
        paragraphStyle = NSMutableParagraphStyle.alloc().init()
        paragraphStyle.setAlignment_(0)  # left
        paragraphStyle.setLineBreakMode_(NSLineBreakByTruncatingTail)
        attributes = {
            NSFontAttributeName: NSFont.systemFontOfSize_(11),
            NSForegroundColorAttributeName: textColour,
            NSParagraphStyleAttributeName: paragraphStyle,
        }
        string = NSMutableAttributedString.alloc().initWithString_attributes_(
            text, attributes
        )
        paren = text.rfind(" (")
        if paren != -1 and text.endswith(")"):
            string.addAttribute_value_range_(
                NSForegroundColorAttributeName,
                textColour.colorWithAlphaComponent_(0.5),
                (paren, len(text) - paren),
            )
        if maxWidth is None:
            string.drawAtPoint_((x, y))
        else:
            # drawInRect_ honours the truncating paragraph style within the width
            string.drawInRect_(NSMakeRect(x, y, maxWidth, 15.0))

    @objc.python_method
    def cellForDrag(self, pt):
        """Like cellIndexAtPoint, but only on a row's label strip, so clicks on the
        glyphs never start a reorder."""
        idx = self.cellIndexAtPoint(pt)
        if idx is None:
            return None
        info = self._gridLayout
        numRows, rowPitch, topMargin = info[2], info[5], info[6]
        _, row = gridColRow(idx, numRows)
        rowTop = topMargin + row * rowPitch
        if pt.y > rowTop + self._labelH:
            return None
        return idx

    @objc.python_method
    def cellIndexAtPoint(self, pt):
        info = self._gridLayout
        if not info:
            return None
        keys, columns, numRows, colW, colGap, rowPitch, topMargin, leftMargin = info
        return gridIndexAtPoint(
            pt.x,
            pt.y,
            len(keys),
            columns,
            numRows,
            colW,
            colGap,
            rowPitch,
            topMargin,
            leftMargin,
        )

    @objc.python_method
    def dropSlotAtPoint(self, pt):
        """Insertion slot (0..count) nearest the drag point in the hovered column."""
        info = self._gridLayout
        if not info:
            return None
        keys, columns, numRows, colW, colGap, rowPitch, topMargin, leftMargin = info
        return gridSlotAtPoint(
            pt.x,
            pt.y,
            len(keys),
            columns,
            numRows,
            colW,
            colGap,
            rowPitch,
            topMargin,
            leftMargin,
        )

    def mouseDown_(self, event):
        try:
            window = self.window()
            if window is not None:
                window.makeKeyAndOrderFront_(None)
                try:
                    window.makeFirstResponder_(self)  # arrow keys step instances
                except Exception:
                    pass
            self._dragIndex = None
            self._dropIndex = None
            self._dragKeysSnapshot = None
            pt = self.convertPoint_fromView_(event.locationInWindow(), None)
            # shift-click (reference lock) goes first so it can't also start a reorder
            if event.modifierFlags() & NSEventModifierFlagShift:
                idx = self.cellIndexAtPoint(pt)
                if idx is not None:
                    self.wrapper.toggleLock(self._gridLayout[0][idx])
                return
            # _dropIndex stays None until a real drag, so a plain click never reorders
            idx = self.cellForDrag(pt)
            self._dragIndex = idx
            info = self._gridLayout
            # snapshot the order: a mid-drag redraw (Live, document switch) can rebuild
            # _gridLayout and make mouseUp reorder the wrong row
            self._dragKeysSnapshot = list(info[0]) if idx is not None else None
        except Exception:
            print(traceback.format_exc())

    def mouseDragged_(self, event):
        try:
            if self._dragIndex is None:
                return
            NSCursor.dragLinkCursor().set()
            pt = self.convertPoint_fromView_(event.locationInWindow(), None)
            # redraw only on slot change: a full-grid redraw per move lags big families
            newDrop = self.dropSlotAtPoint(pt)
            if newDrop == self._dropIndex:
                return
            self._dropIndex = newDrop
            self.setNeedsDisplay_(True)
        except Exception:
            print(traceback.format_exc())

    def mouseUp_(self, event):
        try:
            NSCursor.arrowCursor().set()
            src = self._dragIndex
            dst = self._dropIndex
            snapshot = self._dragKeysSnapshot
            self._dragIndex = None
            self._dropIndex = None
            self._dragKeysSnapshot = None
            info = self._gridLayout
            pt = self.convertPoint_fromView_(event.locationInWindow(), None)
            if src is not None and dst is not None and info:
                keys = list(info[0])
                # grid changed mid-drag: cancel, don't reorder or activate
                if snapshot is not None and snapshot != keys:
                    self.setNeedsDisplay_(True)
                    return
                if (0 <= src < len(keys)) and dst != src and dst != src + 1:
                    self.wrapper.commitReorder(reorderKeys(keys, src, dst))
                    return
                # landed in place: treat as a click
            # shift-click was handled in mouseDown
            if event.modifierFlags() & NSEventModifierFlagShift:
                self.setNeedsDisplay_(True)
                return
            idx = self.cellIndexAtPoint(pt)
            if idx is not None:
                self.wrapper.activateRowKey(info[0][idx])
            self.setNeedsDisplay_(True)
        except Exception:
            print(traceback.format_exc())

    def updateTrackingAreas(self):
        # NSTrackingInVisibleRect makes the area follow scrolling/resize automatically
        existing = getattr(self, "_trackingArea", None)
        if existing is not None:
            self.removeTrackingArea_(existing)
        options = (
            NSTrackingMouseMoved
            | NSTrackingMouseEnteredAndExited
            | NSTrackingActiveInKeyWindow
            | NSTrackingInVisibleRect
        )
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), options, self, None
        )
        self.addTrackingArea_(area)
        self._trackingArea = area
        objc.super(AZSuperPreviewView, self).updateTrackingAreas()

    def mouseMoved_(self, event):
        try:
            if self._dragIndex is not None:
                return  # mid-drag: leave the drag cursor alone
            pt = self.convertPoint_fromView_(event.locationInWindow(), None)
            idx = self.cellIndexAtPoint(pt)
            if idx is not None:
                NSCursor.pointingHandCursor().set()
            else:
                NSCursor.arrowCursor().set()
            # the hovered card draws blue; redraw only on change
            if idx != self._hoverIndex:
                self._hoverIndex = idx
                self.setNeedsDisplay_(True)
        except Exception:
            pass

    def mouseExited_(self, event):
        NSCursor.arrowCursor().set()
        if self._hoverIndex is not None:
            self._hoverIndex = None
            self.setNeedsDisplay_(True)


class ScrollingPreview(VanillaBaseObject):
    nsScrollViewClass = NSScrollView

    def __init__(self, posSize):
        self._plugin = None
        self._glyphsList = []
        self._size = 48
        self._columns = 1  # 0 = Auto (fit window width), 1–10 = fixed columns
        self._showCoords = True
        self._axisOrder = []  # axis-tag sort priority (first = primary)
        self._customOrder = []  # drag order (item key strings)
        self._showGuides = False
        self._showKerning = False
        self._showSpacing = False
        self._showOutlines = False  # x-ray: stroke glyphs instead of filling them
        self._showAnchors = False
        self._showNodes = False
        self._showZones = False
        self._waterfall = bool(Glyphs.defaults[PLUGIN_ID + ".waterfall"])
        self._waterfallLadder = waterfallSizes(WATERFALL_START, WATERFALL_STEPS)
        self._waterfallMultiColumns = bool(
            Glyphs.defaults[PLUGIN_ID + ".waterfallMultiColumns"]
        )
        self._activeOnly = False
        self._lockedKey = ""  # shift-clicked reference row, ghosted behind every row
        self.lockCallback = None
        self._showMasters = False
        self._focusKey = ""  # itemKey of the Waterfall instance ("" = current master)
        self._embedded = False  # in the G4 sidebar tab: one column
        self._liveSig = None  # path-cache key, set by the window's Live check
        self.reorderCallback = None
        self.appearanceCallback = None
        self._foreColour = NSColor.blackColor()
        self._backColour = NSColor.whiteColor()
        self.instances = LRUDict(64)  # instanceIdentity -> interpolated GSFont

        self._setupView(self.nsScrollViewClass, posSize)
        scrollView = self._nsObject
        scrollView.setHasVerticalScroller_(True)
        scrollView.setHasHorizontalScroller_(False)
        scrollView.setDrawsBackground_(True)
        scrollView.setBackgroundColor_(self._backColour)
        # macOS 26 adds a frosted "scroll pocket" at scroll edges that blurs the top
        # row; disallow it on every edge (no-op before macOS 26)
        try:
            if scrollView.respondsToSelector_("setAllowedPocketEdges:"):
                scrollView.setAllowedPocketEdges_(0)
        except Exception:
            pass

        self.docView = AZSuperPreviewView.alloc().initWithFrame_(
            NSMakeRect(0, 0, 100, 100)
        )
        self.docView.setAutoresizingMask_(NSViewWidthSizable)
        self.docView.wrapper = self
        self.docView._gridLayout = None
        self.docView._labelH = 24.0
        self.docView._dragIndex = None
        self.docView._hoverIndex = None
        self.docView._dropIndex = None
        self.docView._dragKeysSnapshot = None
        self.docView.resetPathCaches()
        scrollView.setDocumentView_(self.docView)

    def effectiveShowMasters(self):
        """Waterfall always previews instances; other modes follow the user's choice."""
        return bool(self._showMasters) and not bool(self._waterfall)

    def effectiveWaterfallColumns(self, width):
        """Multi columns: one, plus one per WATERFALL_COLUMN_W pt of width. The G4
        sidebar is too narrow for more than one."""
        if not bool(self._waterfallMultiColumns) or bool(self._embedded):
            return 1
        return 1 + int(width // WATERFALL_COLUMN_W)

    def effectiveActiveOnly(self):
        """Waterfall shows all instances; other modes follow the user's choice."""
        return bool(self._activeOnly) and not bool(self._waterfall)

    def commitReorder(self, orderedKeys):
        """_customOrder holds both modes' keys ('master:' prefix), so replace only the
        current mode's partition."""
        showMasters = bool(self._showMasters)
        other = [k for k in self._customOrder if k.startswith("master:") != showMasters]
        self._customOrder = list(orderedKeys) + other
        self.reorderCallback(list(self._customOrder))
        self.docView.setNeedsDisplay_(True)

    def toggleLock(self, key):
        """Pin/unpin the reference row ghosted behind every row."""
        self._lockedKey = "" if self._lockedKey == key else key
        self.lockCallback(self._lockedKey)
        self.docView.setNeedsDisplay_(True)

    def activateRowKey(self, key):
        """Plain click: select the row's master, or show its instance in Preview."""
        font = Glyphs.font
        if font is None:
            return
        showMasters = self.effectiveShowMasters()  # Waterfall forces instances
        for item in displayItems(font, showMasters, self.effectiveActiveOnly()):
            if itemKey(item, showMasters) == key:
                if showMasters:
                    self.selectMaster(font, item)
                else:
                    self.previewInstance(font, item)
                return

    def selectMaster(self, font, master):
        """font.selectedFontMaster has no setter, so drive it through masterIndex."""
        try:
            masters = list(font.masters)
            for i, m in enumerate(masters):
                if m.id == master.id:
                    font.masterIndex = i
                    return
        except Exception:
            pass

    def previewInstance(self, font, instance):
        try:
            tab = font.currentTab
            if tab is None:
                return
            tab.previewInstances = instance
            if not tab.previewHeight or tab.previewHeight < 1:
                tab.previewHeight = 120
        except Exception:
            pass

    def setBackgroundColour(self, colour):
        self._backColour = colour
        self._nsObject.setBackgroundColor_(self.effectiveBackColour())

    def effectiveBackColour(self):
        """Waterfall paints on the card grey: a theme-coloured page under the ramp read
        as a second box inside the card."""
        if bool(self._waterfall):
            return _az_card_fill()
        return self._backColour

    def effectiveForeColour(self):
        """The Waterfall card grey is dark in both themes, so its ink stays white."""
        if bool(self._waterfall):
            return NSColor.whiteColor()
        return self._foreColour

    def clearCache(self):
        # sized to the front font, floor 64 (also when Glyphs.font is None)
        self.instances = LRUDict(max(64, 2 * len(staticInstances(Glyphs.font))))
        self.docView.resetPathCaches()
        self.docView._maxRunKey = None  # widest-run memo; re-flow Auto columns

    def setGlyphs(self, names):
        """Set the sample glyphs; on change drop the path caches (keyed by glyph list,
        so stale entries would pile up)."""
        changed = names != self._glyphsList
        self._glyphsList = list(names)
        if changed:
            self.docView.resetPathCaches()

    def updateContentSize(self):
        scrollView = self._nsObject
        # mode may have just flipped: re-assert the page colour
        scrollView.setBackgroundColor_(self.effectiveBackColour())
        visible = scrollView.contentSize()
        width = visible.width
        # called per live-resize event: Glyphs.font costs ~0.6 ms (XPC); use the cache
        font = self._plugin._cur_font()
        if bool(self._waterfall):
            height = self.docView.waterfallContentHeight(font, float(self._size))
            self.docView.setFrameSize_(NSMakeSize(width, max(height, visible.height)))
            self.docView.setNeedsDisplay_(True)
            return
        count = len(displayItems(font, self._showMasters, self._activeOnly))
        columns = self.docView.effectiveColumns(
            font, self._glyphsList, float(self._size), width, count
        )
        height = self.docView.contentHeight(font, count, float(self._size), columns)
        self.docView.setFrameSize_(NSMakeSize(width, max(height, visible.height)))
        self.docView.setNeedsDisplay_(True)

    def redraw(self):
        self.docView.setNeedsDisplay_(True)

    def outlineSignature(self):
        """Hash of the shown glyphs' source-master nodes, components, anchors, widths
        and kerning, to skip Live redraws when nothing changed. Never interpolates."""
        font = self._plugin._cur_font()
        if font is None:
            return None
        masterIds = [m.id for m in font.masters]
        names = list(self._glyphsList)
        layers = {}  # (name, master id) -> layer, reused by the kerning pass
        parts = []
        for name in dict.fromkeys(names):
            glyph = font.glyphs[name]
            if glyph is None:
                continue
            for mid in masterIds:
                layer = glyph.layers[mid]
                if layer is None:
                    continue
                layers[(name, mid)] = layer
                try:
                    for path in layer.paths:
                        for node in path.nodes:
                            pos = node.position
                            parts.append((pos.x, pos.y, node.type))
                    for comp in layer.components:
                        try:
                            transform = tuple(comp.transform)
                        except Exception:
                            transform = None
                        parts.append((comp.componentName, transform))
                    for anchor in layer.anchors.values():  # G4 iterates anchor names
                        apos = anchor.position
                        parts.append((anchor.name, apos.x, apos.y))
                    parts.append((name, mid, round(layer.width, 2)))
                except Exception:
                    parts.append((name, mid, "err"))
        # kerning touches no outline: fold adjacent-pair kern values in so a kerning
        # edit also triggers a Live redraw
        for left, right in dict.fromkeys(zip(names, names[1:])):
            for mid in masterIds:
                leftLayer = layers.get((left, mid))
                rightLayer = layers.get((right, mid))
                if leftLayer is None or rightLayer is None:
                    continue
                for rtl in (False, True):  # detect edits to either LTR or RTL kerning
                    try:
                        kernValue = getKernValue(leftLayer, rightLayer, rtl)
                        parts.append(
                            ("kern", rtl, mid, left, right, round(kernValue, 2))
                        )
                    except Exception:
                        pass
        return hash(tuple(parts))


# Glyphs 4 left sidebar: Glyphs instantiates the registered controller class once per
# Edit view; the single plugin UI hops into whichever tab is frontmost.
_AZSP_SINGLETON = []  # [plugin instance], set in start(); PyObjC forbids weakref

# Glyphs checks conformsToProtocol:, and a PyObjC subclass must declare the protocol at
# class creation. Outside Glyphs 4 the protocol doesn't exist -> empty list.
try:
    _AZSP_SIDEBAR_PROTOCOLS = [
        objc.protocolNamed("GSEditViewSidebarControllerProtocol")
    ]
except Exception:
    _AZSP_SIDEBAR_PROTOCOLS = []


class AZSPSidebarController(NSViewController, protocols=_AZSP_SIDEBAR_PROTOCOLS):
    @classmethod
    def title(cls):  # +title is the tab label; -title is set in setController_
        return _TAB_TITLE

    @classmethod
    def tabBarImage(cls):
        if _TAB_IMAGE_CACHE:
            return _TAB_IMAGE_CACHE[0]
        # AZ suite art, else an SF Symbol
        img = _az_icon(AZ_TAB_ICON_H)
        if img is None:
            img = symbolImage(SFNAME_PANEL, AZ_TAB_ICON_H)
        if img is not None:
            if img.respondsToSelector_(b"setAccessibilityDescription:"):
                img.setAccessibilityDescription_(_TAB_TITLE)
            _TAB_IMAGE_CACHE.append(img)
        return img

    def loadView(self):
        container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 260, 400))
        self.setView_(container)
        # Hint shown only while the panel lives in the floating window. Starts hidden:
        # loadView runs before adoptSidebarHost, so a visible label flashed on load.
        try:
            TextFieldCls = objc.lookUpClass("NSTextField")
            lbl = TextFieldCls.wrappingLabelWithString_("")
            lbl.setSelectable_(False)
            # centre via the attributed string's paragraph style: a plain setAlignment_
            # doesn't reliably centre a wrapping label's second line
            style = NSMutableParagraphStyle.alloc().init()
            style.setAlignment_(
                1
            )  # NSTextAlignmentCenter (macOS 10.12+: center=1, right=2)
            lbl.setAttributedStringValue_(
                NSAttributedString.alloc().initWithString_attributes_(
                    "Open in\nFloating Window",
                    {
                        NSParagraphStyleAttributeName: style,
                        NSFontAttributeName: NSFont.systemFontOfSize_(13.0),
                        NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
                    },
                )
            )
            lbl.setIdentifier_("AZSPSidebarPlaceholder")
            lbl.setHidden_(True)  # releaseSidebarHost reveals it
            lbl.sizeToFit()
            b = container.bounds()
            f = lbl.frame()
            lbl.setFrameOrigin_(
                (
                    (b.size.width - f.size.width) / 2.0,
                    (b.size.height - f.size.height) / 2.0,
                )
            )
            lbl.setAutoresizingMask_(
                NSViewMinXMargin
                | NSViewMaxXMargin
                | NSViewMinYMargin
                | NSViewMaxYMargin
            )
            container.addSubview_(lbl)
        except Exception:
            pass

    def viewDidAppear(self):
        try:
            objc.super(AZSPSidebarController, self).viewDidAppear()
        except Exception:
            pass
        plug = _AZSP_SINGLETON[0] if _AZSP_SINGLETON else None
        if plug is not None:
            try:
                plug._editSidebarCtrl = self.controller()  # for collapse/expand
            except Exception:
                pass
            try:
                plug.adoptSidebarHost(self.view())
            except Exception:
                print(traceback.format_exc())

    def viewDidDisappear(self):
        try:
            objc.super(AZSPSidebarController, self).viewDidDisappear()
        except Exception:
            pass
        plug = _AZSP_SINGLETON[0] if _AZSP_SINGLETON else None
        if plug is not None:
            try:
                plug.releaseSidebarHost(self.view())
            except Exception:
                print(traceback.format_exc())

    def setController_(self, gsController):
        # Glyphs shows the instance -title as the tab tooltip; +title is only the label
        self.setTitle_(_TAB_TITLE)
        # strong ref (protocol says weak, PyObjC can't weakref ObjC objects); worst
        # case one retained edit-VC per closed tab
        self._gsController = gsController

    def controller(self):
        return getattr(self, "_gsController", None)


# ──────────────────────────────────────────────────────────────────────────────
# The plugin's face
# ──────────────────────────────────────────────────────────────────────────────
# One piece of art per plugin, bundled as `az-icon.pdf`, drawn wherever the
# plugin identifies itself: its item in Window ▸ AZ Toolkit and its tab in the
# Glyphs 4 left sidebar. It replaces the SF Symbol each surface used to pick on
# its own — the suite reads as a suite only if a plugin wears one face.
#
# TEMPLATE, always. The art is solid black on transparency, which is invisible
# against a dark menu and against a selected row. A template image is inked by
# AppKit with the surrounding text colour instead of its own, so the same file
# is black in light mode, white in dark mode, and white while highlighted.
#
# Inlined identically in every module that needs it — plugins share one
# sys.modules, so a bare `import sidebar` is a collision waiting to happen —
# so KEEP THE COPIES IN SYNC.

AZ_ICON_FILE = "az-icon.pdf"
_AZ_ICON_CACHE: dict = {}


def _az_icon(point_size: float = 14.0):
    """The bundled plugin icon as a template NSImage `point_size` tall, or None.

    Cached per size: an NSImage is mutable and carries its own size, so two
    surfaces asking for different sizes must not be handed the same object —
    that is how one caller's resize silently shrinks another's icon.
    """
    cached = _AZ_ICON_CACHE.get(point_size)
    if cached is not None:
        return cached
    try:
        import os

        from AppKit import NSImage

        # The art sits in the bundle's Resources/, but the module asking for it
        # may be a package DEEPER than that (azkeyring/sidebar.py), so walk up.
        folder = os.path.dirname(os.path.abspath(__file__))
        path = None
        for _ in range(3):
            candidate = os.path.join(folder, AZ_ICON_FILE)
            if os.path.exists(candidate):
                path = candidate
                break
            folder = os.path.dirname(folder)
        if path is None:
            return None
        image = NSImage.alloc().initByReferencingFile_(path)
        if image is None or not image.isValid():
            return None
        # `size()` answers an NSSize STRUCT, never a tuple — testing for a tuple
        # here is what once left a 200pt tile at its natural size in a menu row.
        natural = image.size()
        if natural.height:
            image.setSize_((natural.width * point_size / natural.height, point_size))
        image.setTemplate_(True)
        _AZ_ICON_CACHE[point_size] = image
        return image
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Window ▸ AZ Toolkit
# ──────────────────────────────────────────────────────────────────────────────
# The suite shows ONE item at the root of the Window menu — "AZ Toolkit" — with
# every AZ plugin as a child, plus a link to the site at the bottom. Whichever
# plugin loads first builds the submenu; the rest find it by title and insert
# themselves alphabetically, so load order never shows.
#
# Child titles drop the "AZ " prefix (the parent already says it), matching the
# AZ Toolkit Settings hub, which labels its tabs the same way.
#
# This block is inlined identically in every AZ plugin — separate bundles share
# no module — so KEEP THE COPIES IN SYNC.

AZ_TOOLKIT_MENU_TITLE = "AZ Toolkit"
AZ_TOOLKIT_SITE_TITLE = "More AZ plugins…"
AZ_TOOLKIT_SITE_URL = "https://azerty-typefaces.com"
# NSMenuItem does not retain its target, and a plugin reload drops the module
# global that would hold it — so the site link's target is parked on the main
# thread's dictionary, which lives as long as the app does. Same rendezvous the
# AZ Toolkit Settings hub uses ("AZToolkit.SettingsHub.v1").
AZ_TOOLKIT_OPENER_KEY = "AZToolkit.WindowMenu.SiteOpener.v1"


def _az_toolkit_title(name: str) -> str:
    """A child item's label: the plugin name without its "AZ " prefix."""
    return name[3:] if name.startswith("AZ ") else name


def _az_toolkit_site_opener():
    """The retained object behind "More AZ plugins…". One per app session."""
    import objc
    from AppKit import NSWorkspace
    from Foundation import NSObject, NSThread, NSURL

    store = NSThread.mainThread().threadDictionary()
    opener = store.objectForKey_(AZ_TOOLKIT_OPENER_KEY)
    if opener is not None:
        return opener
    try:
        # Another AZ plugin already defined it: ObjC class names are global to
        # the process and all Python plugins share one interpreter, so
        # re-declaring the class raises instead of shadowing.
        opener_class = objc.lookUpClass("AZToolkitSiteOpener")
    except objc.error:

        class AZToolkitSiteOpener(NSObject):
            def openAZSite_(self, sender) -> None:
                NSWorkspace.sharedWorkspace().openURL_(
                    NSURL.URLWithString_(AZ_TOOLKIT_SITE_URL)
                )

        opener_class = AZToolkitSiteOpener
    opener = opener_class.alloc().init()
    store.setObject_forKey_(opener, AZ_TOOLKIT_OPENER_KEY)
    return opener


def _az_toolkit_menu():
    """Find-or-create ``Window ▸ AZ Toolkit`` and return its NSMenu, or None if
    the Window menu is not the shape we expect."""
    from AppKit import NSImage, NSMenu, NSMenuItem

    try:
        window_menu = Glyphs.menu[WINDOW_MENU].submenu()
        existing_items = list(window_menu.itemArray())
    except Exception:
        return None
    for item in existing_items:
        if str(item.title() or "") == AZ_TOOLKIT_MENU_TITLE:
            return item.submenu()

    submenu = NSMenu.alloc().initWithTitle_(AZ_TOOLKIT_MENU_TITLE)
    parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        AZ_TOOLKIT_MENU_TITLE, None, ""
    )
    parent.setSubmenu_(submenu)
    # The same symbol the AZ Toolkit pane carries in Settings, so the two
    # surfaces read as one thing. Guarded: a host without SF Symbols has no
    # such selector, and an icon is never worth losing the whole menu over.
    try:
        icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "wrench.and.screwdriver", AZ_TOOLKIT_MENU_TITLE
        )
        if icon is not None:
            icon.setTemplate_(True)
            icon.setSize_((14.0, 14.0))  # menu-item icon metrics
            parent.setImage_(icon)
    except Exception:
        pass

    # Same reasoning as the icon: if the opener cannot be built the plugin items
    # still belong in the menu, so only the link is dropped.
    try:
        site_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            AZ_TOOLKIT_SITE_TITLE, "openAZSite:", ""
        )
        site_item.setTarget_(_az_toolkit_site_opener())
        submenu.addItem_(NSMenuItem.separatorItem())
        submenu.addItem_(site_item)
    except Exception:
        pass

    window_menu.addItem_(NSMenuItem.separatorItem())
    window_menu.addItem_(parent)
    return submenu


def _install_az_toolkit_item(menu_item) -> None:
    """Put a plugin's item in the AZ Toolkit submenu, alphabetically, above the
    trailing separator + site link.

    IDEMPOTENT BY TITLE: reloading a plugin while Glyphs runs calls ``start``
    again on a menu that already holds the item, and AppKit will happily show
    two of them.
    """
    from AppKit import NSMenuItem

    submenu = _az_toolkit_menu()
    if submenu is None:  # unexpected menu shape — do not lose the item
        Glyphs.menu[WINDOW_MENU].append(NSMenuItem.separatorItem())
        Glyphs.menu[WINDOW_MENU].append(menu_item)
        return
    title = str(menu_item.title() or "")
    for existing in list(submenu.itemArray()):
        if str(existing.title() or "") == title:
            submenu.removeItem_(existing)
    items = list(submenu.itemArray())
    # The trailing separator is the end of the plugin block; the site link sits
    # below it and always stays last.
    end = next(
        (i for i, item in enumerate(items) if item.isSeparatorItem()), len(items)
    )
    insert_at = next(
        (i for i in range(end) if str(items[i].title() or "") > title), end
    )
    submenu.insertItem_atIndex_(menu_item, insert_at)


class AZSuperPreviewWindow(GeneralPlugin):
    @objc.python_method
    def settings(self):
        self.name = "AZ Super Preview"

    @objc.python_method
    def start(self):
        _AZSP_SINGLETON[:] = [self]
        # host state read by callbacks that can fire before showWindow_ builds the UI
        self.w = None
        self._closed = True
        self._hidden = False
        self._hiddenForFontView = False
        self._sidebarMode = False
        self._sidebarHost = None
        self._sidebarTabContainer = None
        self._splitHome = None
        # G4-only native left-sidebar tab (API absent on G3); gates the sidebar toggle
        self._sidebarCapable = False
        try:
            cbh = objc.lookUpClass("GSCallbackHandler")
            if hasattr(
                cbh, "addEditViewLeftSidebarController_"
            ):  # PyObjC name; respondsToSelector_ would need the colon selector
                cbh.addEditViewLeftSidebarController_(AZSPSidebarController)
                self._sidebarCapable = True
        except Exception:
            traceback.print_exc()
        newMenuItem = NSMenuItem.new()
        newMenuItem.setTitle_(_az_toolkit_title("AZ Super Preview"))
        newMenuItem.setImage_(_az_icon(16.0))
        newMenuItem.setAction_(self.showWindow_)
        newMenuItem.setTarget_(self)
        try:
            _install_az_toolkit_item(newMenuItem)
        except Exception:
            traceback.print_exc()
        try:
            Glyphs.addCallback(self.interfaceDidUpdate_, UPDATEINTERFACE)
        except Exception:
            pass

    def interfaceDidUpdate_(self, sender):
        # UPDATEINTERFACE fires constantly; act only when the active tab changed
        try:
            if self.w is None:
                return  # never opened: nothing to close, sync or follow
            if self._frontDocIsProject():
                # a .glyphsproject has no Edit canvas: close fully, no auto-restore
                if not self._hidden and not self._closed:
                    self._closePanel()
                return
            font = self._cur_font()
            tab = font.currentTab if font else None
            if tab is not getattr(self, "_lastTab", None):
                self._lastTab = tab
                self._syncPanelToTab()
            else:
                self._followMasterInWaterfall(font)
        except Exception:
            pass

    @objc.python_method
    def _followMasterInWaterfall(self, font):
        """Waterfall on "Current master": redraw when the Edit view's master changes."""
        try:
            pv = self.gP.preview
            if pv._waterfall and not pv._focusKey:
                mi = font.masterIndex
                if mi != getattr(self, "_wfMasterIdx", None):
                    self._wfMasterIdx = mi
                    pv.redraw()
        except Exception:
            pass

    @objc.python_method
    def _cur_font(self):
        """Glyphs.font costs ~0.6 ms (WindowServer XPC); cached 0.2 s.
        changeDocument_ reseeds it, so a document switch is never stale."""
        now = time.time()
        if now - getattr(self, "_fontStamp", 0.0) > 0.2:
            self._fontCached = Glyphs.font
            self._fontStamp = now
        return getattr(self, "_fontCached", None)

    @objc.python_method
    def _isEditTab(self):
        try:
            font = self._cur_font()
            return bool(font and font.currentTab)
        except Exception:
            return False

    @objc.python_method
    def _frontDocIsProject(self):
        """True if the main or key window is a .glyphsproject (not a background one)."""
        try:
            ProjCls = objc.lookUpClass("GSProjectDocument")
        except Exception:
            return False
        try:
            app = objc.lookUpClass("NSApplication").sharedApplication()
            for win in (app.mainWindow(), app.keyWindow()):
                if win is None:
                    continue
                try:
                    wc = win.windowController()
                    doc = wc.document() if wc is not None else None
                    if doc is not None and doc.isKindOfClass_(ProjCls):
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False

    @objc.python_method
    def _syncPanelToTab(self):
        """Hide a live panel in Font view and restore it on the next Edit tab."""
        try:
            isEdit = self._isEditTab() and self._editWindow() is not None
            panelLive = self.w is not None and not self._closed
            if isEdit:
                if self._hiddenForFontView and panelLive:
                    self._hiddenForFontView = False
                    if self._hidden:
                        self._wake()
                    try:
                        nsw = self.w.getNSWindow()
                        if nsw is not None:
                            nsw.orderFront_(None)  # don't steal key focus
                    except Exception:
                        pass
            else:
                # a hidden panel here was closed by the user: nothing to restore later
                if panelLive and not self._hiddenForFontView and not self._hidden:
                    try:
                        nsw = self.w.getNSWindow()
                        if nsw is not None:
                            nsw.orderOut_(None)
                    except Exception:
                        pass
                    self._hidden = True
                    self._hiddenForFontView = True
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _closePanel(self):
        """Hide the panel's window, kept warm so re-showing it reuses the built UI and
        interpolation caches instead of rebuilding."""
        try:
            self._homeSplitInFloating(self.w.split.getNSSplitView())
            self.gP.preview.updateContentSize()
        except Exception:
            print(traceback.format_exc())
        try:
            # hide, don't close(): keeps the UI and caches warm
            self.w.getNSWindow().orderOut_(None)
        except Exception:
            print(traceback.format_exc())
        # callbacks stay registered but no-op while _hidden; _wake() clears it
        self._hidden = True
        # closed: the next Edit tab must not reopen it
        self._hiddenForFontView = False

    @objc.python_method
    def _wake(self):
        """Refresh a warm-hidden panel before it shows again: the work its callbacks
        skipped while hidden (full cache rebuild only if the document changed)."""
        self._hidden = False
        try:
            if self._dirtyWhileHidden:
                self._dirtyWhileHidden = False
                self.gP.preview.clearCache()
                self.gP.preview._liveSig = None
                self.refreshAxisOrder()
            self._lastTabList = None  # force a tab re-mirror
            self.syncFromTab_(None)
            self.gP.preview.updateContentSize()
            self.liveCheck_(None)
        except Exception:
            print(traceback.format_exc())

    def showWindow_(self, sender):
        try:
            # Already open: bring it front; a second build would leak the first
            # window's registered callbacks until quit
            existing = self.w
            if existing is not None and not self._closed:
                if self._hidden:
                    self._wake()
                # in the G4 sidebar tab, the menu item means "pop out to floating"
                if self._sidebarMode:
                    self.floatFromSidebar_(None)
                    return
                try:
                    existing.show()
                    nsw = existing.getNSWindow()
                    if nsw is not None:
                        nsw.makeKeyAndOrderFront_(None)
                except Exception:
                    pass
                return

            edY = 22  # sample-field height
            width, height = 600, 600
            self._lastTabList = None
            self._closed = False
            self._hidden = False  # closed but kept alive (warm)
            self._hiddenForFontView = False
            self._dirtyWhileHidden = False  # document switched while hidden
            self._sidebarMode = False  # UI lives in the G4 left-sidebar tab
            self._sidebarHost = None
            self._splitHome = None  # (superview, frame, mask) to restore on release
            self._stackedTopH = TOOLBAR_H  # settings band height

            savedText = Glyphs.defaults[PLUGIN_ID + ".text"] or "Hamburgefonstiv"
            savedSize = self._intDefault(".size", 48, 30, 160)
            savedColumns = self._intDefault(".columns", 1, 0, 10)
            savedCustomOrder = list(Glyphs.defaults[PLUGIN_ID + ".customOrder"] or [])
            savedShowMasters = bool(Glyphs.defaults[PLUGIN_ID + ".showMasters"])
            savedActiveOnly = bool(Glyphs.defaults[PLUGIN_ID + ".activeOnly"])
            savedLockedKey = Glyphs.defaults[PLUGIN_ID + ".lockedKey"] or ""
            savedFocusKey = Glyphs.defaults[PLUGIN_ID + ".focusKey"] or ""
            savedWfStart = self._intDefault(".waterfallStart", WATERFALL_START, 4, 200)
            savedWfSteps = self._intDefault(".waterfallSteps", WATERFALL_STEPS, 1, 40)

            self.w = FloatingWindow(
                (width, height),
                self.name,
                autosaveName=PLUGIN_ID + ".mainwindow",
                minSize=(560, MIN_WINDOW_H),
            )
            self.w.bind("close", self.windowClosed_)
            self.w.bind("resize", self.windowResized_)

            # card padding included so controls align with card content in other AZ tabs
            sp = AZ_SIDE_MARGIN + AZ_CARD_PAD_X
            lblW = 34  # "Size" label; short so the slider is longer
            ctlX = sp + lblW + 2
            sIcon = 18
            gearInset = sp + sIcon + 6  # controls clear the right-edge button column
            eyeX = -(sp + sIcon)
            self.gS = Group((0, 0, -0, -0))
            # card as first subview so every control draws above it
            try:
                card = AZSPSettingsCardView.alloc().initWithFrame_(
                    self.gS._nsObject.bounds()
                )
                card.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
                self.gS._nsObject.addSubview_(card)
                self._settingsCard = card
            except Exception:
                self._settingsCard = None
            # _layoutSidebar() places these; the frames below are only defaults
            self.gS.modeSeg = SegmentedButton(
                (sp, 60, -sp, 16),
                [dict(title="Masters"), dict(title="Instances")],
                callback=self.modeChanged_,
                sizeStyle="small",
            )
            self.gS.modeSeg.set(0 if savedShowMasters else 1)
            self.gS.sizeLabel = TextBox((sp, 40, lblW, 14), "Size", sizeStyle="small")
            self.gS.sizeSlider = Slider(
                (ctlX, 38, -(sp + 44), 16),
                minValue=30,
                maxValue=160,
                value=savedSize,
                sizeStyle="mini",
                callback=self.sizeSliderChanged_,
            )
            self.gS.sizeText = EditText(
                (-(sp + 38), 37, 38, 19),
                text=str(savedSize),
                sizeStyle="small",
            )
            # Up/Down arrows step the size (Shift = ±10)
            self._sizeStepDelegate = AZSizeStepDelegate.alloc().initWithOwner_(self)
            try:
                self.gS.sizeText._nsObject.setDelegate_(self._sizeStepDelegate)
            except Exception:
                pass
            self.gS.columnsLabel = iconLabel((sp, 61, 22, 20), SFNAME_COLUMN, SF_COLUMN)
            self.gS.columnsText = EditText(
                (sp + 26, 60, 40, 21),
                text=("Auto" if savedColumns == 0 else str(savedColumns)),
                readOnly=True,  # display-only; the stepper drives it
                sizeStyle="regular",
            )
            self.gS.columnsStepper = Stepper(
                (sp + 66, 60, 19, 22),
                value=savedColumns,
                minValue=0,
                maxValue=10,
                increment=1,
                callback=self.columnsStepperChanged_,
            )
            try:
                self.gS.columnsStepper._nsObject.setControlSize_(
                    1
                )  # NSControlSizeSmall
            except Exception:
                pass
            # Waterfall ladder: smallest size and how many; _layoutToolbar places them
            for name, title, value, lo, hi in (
                ("Start", "Start", savedWfStart, 4, 200),
                ("Steps", "Steps", savedWfSteps, 1, 40),
            ):
                label = TextBox((sp, 60, 34, 14), title, sizeStyle="small")
                field = EditText(
                    (sp, 60, 34, 19),
                    text=str(value),
                    callback=getattr(self, f"waterfall{name}Changed_"),
                    sizeStyle="small",
                )
                arrows = Stepper(
                    (sp, 60, 13, 19),
                    value=value,
                    minValue=lo,
                    maxValue=hi,
                    increment=1,
                    callback=getattr(self, f"waterfall{name}StepperChanged_"),
                )
                try:
                    arrows._nsObject.setControlSize_(1)  # NSControlSizeSmall
                except Exception:
                    pass
                setattr(self.gS, f"waterfall{name}Label", label)
                setattr(self.gS, f"waterfall{name}", field)
                setattr(self.gS, f"waterfall{name}Stepper", arrows)
            for ctl in self._waterfallScaleControls():
                ctl.show(False)
            self.gS.waterfallMultiColumns = CheckBox(
                (sp, 90, -sp, 16),
                "Multi columns",
                value=bool(Glyphs.defaults[PLUGIN_ID + ".waterfallMultiColumns"]),
                callback=self.waterfallMultiColumnsChanged_,
                sizeStyle="small",
            )
            self.gS.waterfallMultiColumns.show(False)
            self.gS.activeCheckBox = CheckBox(
                (sp, 90, -sp, 16),
                "Active only",
                value=savedActiveOnly,
                callback=self.activeToggle_,
                sizeStyle="small",
            )
            # narrow hosts' mode switch; the floating window uses _tbMode
            self.gS.embedMode = SegmentedButton(
                (sp, 90, -sp, 16),
                [dict(title="Overview"), dict(title="Waterfall")],
                callback=self.dispModeChanged_,
                sizeStyle="small",
            )
            self.gS.embedMode.set(
                1 if bool(Glyphs.defaults[PLUGIN_ID + ".waterfall"]) else 0
            )
            self.gS.embedMode.show(False)
            self.gS.focusPopup = PopUpButton(
                (sp + 66, 88, -sp, 20),
                [],
                callback=self.focusPickerChanged_,
                sizeStyle="small",
            )
            self.gS.edit = EditText(
                (sp, 36, -gearInset, edY), text=savedText, callback=self.textChanged_
            )
            self.gS.sidebarToggle = Button(
                (eyeX, 37, TOGGLE_BTN_W, edY),
                "⇄",
                callback=self.toggleSidebarFloat_,
            )
            setButtonImage(self.gS.sidebarToggle, symbolImage(SFNAME_SWAP, 11.5), "⇄")
            # G4-only: _placeTextRow shows it when _sidebarCapable
            self.gS.sidebarToggle.show(False)

            # Floating-window NSToolbar twins of the gS controls (same state methods);
            # built in _buildFloatToolbar, kept in step by _syncFloatToolbar
            self._tbMode = SegmentedButton(
                (0, 0, 214, 24),
                [dict(title="Overview"), dict(title="Waterfall")],
                callback=self.dispModeChanged_,
                sizeStyle="regular",
            )
            try:
                self._tbMode._nsObject.setFocusRingType_(1)  # NSFocusRingTypeNone
            except Exception:
                pass
            self._tbSwitch = Button(
                (0, 0, TOGGLE_BTN_W, 22), "⇄", callback=self.toggleSidebarFloat_
            )
            setButtonImage(self._tbSwitch, symbolImage(SFNAME_SWAP, 11.5), "⇄")
            self._tbPhone = Button(
                (0, 0, TOGGLE_BTN_W, 22), "▦", callback=self.phoneQRClicked_
            )
            setButtonImage(self._tbPhone, symbolImage("qrcode", 13.0), "▦")
            self._tbPhone._nsObject.setToolTip_("Preview on remote device (QR code)")
            self._tbView = Button(
                (0, 0, TOGGLE_BTN_W, 22), "View", callback=self.showViewOptions_
            )
            setButtonImage(self._tbView, symbolImage(SFNAME_VIEW, 10.5), "View")
            self._tbView._nsObject.setToolTip_("View options")

            self.gP = Group((0, 0, -0, -0))
            self.gP.preview = ScrollingPreview((0, 0, -0, -0))
            # the docView reaches the plugin's cached font through this
            self.gP.preview._plugin = self
            # -1 = NSWindowBelow: the card frames the scroll view instead of covering it
            try:
                pcard = AZSPPreviewCardView.alloc().initWithFrame_(
                    self.gP._nsObject.bounds()
                )
                pcard.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
                self.gP._nsObject.addSubview_positioned_relativeTo_(pcard, -1, None)
                pcard.setHidden_(True)  # no card while floating; see _setPreviewCard
                self._previewCard = pcard
            except Exception:
                self._previewCard = None
            self.gP.preview._size = savedSize
            self.gP.preview._columns = savedColumns
            self.gP.preview._showMasters = savedShowMasters
            self.gP.preview._customOrder = savedCustomOrder
            for attr, _title, _indent in VIEW_OPTIONS:
                saved = Glyphs.defaults[PLUGIN_ID + "." + attr[1:]]
                # axis values show until turned off; every overlay starts off
                on = bool(saved) if saved is not None else attr == "_showCoords"
                setattr(self.gP.preview, attr, on)
            self.gP.preview._activeOnly = savedActiveOnly
            self.gP.preview._lockedKey = savedLockedKey
            self.gP.preview._focusKey = savedFocusKey
            self.gP.preview._waterfallLadder = waterfallSizes(
                savedWfStart, savedWfSteps
            )
            self.gP.preview.reorderCallback = self.onInstancesReordered
            self.gP.preview.lockCallback = self.onLockChanged
            self._layoutSidebar()

            self.w.split = SplitView(
                (0, 0, -0, -0),
                [
                    dict(
                        view=self.gS,
                        identifier="settings",
                        size=TOOLBAR_H,
                        minSize=TOOLBAR_H,
                        maxSize=TOOLBAR_H,
                        canCollapse=False,
                    ),
                    dict(
                        view=self.gP,
                        identifier="preview",
                        minSize=300,
                        canCollapse=False,
                    ),
                ],
                isVertical=False,
                dividerStyle="thin",
                dividerColor=NSColor.clearColor(),  # both panes draw their own card
            )

            self.gP.preview.appearanceCallback = self.applyTheme
            self.applyTheme()
            self.gP.preview.setGlyphs(self.makeList(savedText))
            self.refreshAxisOrder()
            self._stripFocusRings(self.w.split.getNSSplitView())

            self.w.open()
            nsw = self.w.getNSWindow()
            if nsw is not None:
                # vanilla's becomesKeyOnlyIfNeeded lets only text fields make it key
                nsw.setBecomesKeyOnlyIfNeeded_(False)
            self.gP.preview.updateContentSize()
            self._layoutSidebar()  # again, now the window has its real size
            self._buildFloatToolbar()
            self._applyMinHeight()

            Glyphs.addCallback(self.changeDocument_, DOCUMENTACTIVATED)
            Glyphs.addCallback(self.liveEditChanged_, UPDATEINTERFACE)
            # mirror the current Edit tab whenever the sample field is empty
            Glyphs.addCallback(self.syncFromTab_, UPDATEINTERFACE)
            self.syncFromTab_(None)
            # interfaceDidUpdate_ skipped tab tracking until now: start from this tab
            font = self._cur_font()
            self._lastTab = font.currentTab if font else None
            self._restoreWindowFrame()
        except Exception:
            print(traceback.format_exc())
            Glyphs.showMacroWindow()

    @objc.python_method
    def _restoreWindowFrame(self):
        """Restore the frame saved by windowClosed_; runs last, so the saved size wins."""
        try:
            raw = Glyphs.defaults[PLUGIN_ID + ".winFrame"]
            if not raw:
                return
            x, y, w, h = json.loads(raw)
            nsw = self.w.getNSWindow()
            if nsw is None:
                return
            nsw.setFrame_display_(
                NSMakeRect(x, y, max(560.0, float(w)), max(MIN_WINDOW_H, float(h))),
                True,
            )
        except Exception:
            pass

    @objc.python_method
    def _buildFloatToolbar(self):
        """Attach the floating window's native NSToolbar (built once)."""
        try:
            if getattr(self, "_floatToolbarBuilt", False):
                return
            from AppKit import NSToolbarFlexibleSpaceItemIdentifier

            # title as the first item, since the real window title is hidden below
            if getattr(self, "_tbTitle", None) is None:
                self._tbTitle = TextBox((0, 0, 140, 22), "AZ Super Preview")
                try:
                    tnf = self._tbTitle._nsObject
                    tnf.setFont_(NSFont.boldSystemFontOfSize_(13.0))
                    tnf.sizeToFit()
                except Exception:
                    pass
            # one item per button so macOS 26 gives each its own glass capsule;
            # borderless so the toolbar glass is the only background
            for _b in (self._tbView, self._tbPhone, self._tbSwitch):
                try:
                    _b._nsObject.setBordered_(False)
                except Exception:
                    pass
            items = [
                dict(itemIdentifier="AZtitle", label="", view=self._tbTitle._nsObject),
                dict(
                    itemIdentifier="AZmode", label="Mode", view=self._tbMode._nsObject
                ),
                dict(itemIdentifier=NSToolbarFlexibleSpaceItemIdentifier),
                dict(
                    itemIdentifier="AZview", label="View", view=self._tbView._nsObject
                ),
                dict(
                    itemIdentifier="AZphone",
                    label="Remote",
                    view=self._tbPhone._nsObject,
                ),
            ]
            if self._sidebarCapable:
                items.append(
                    dict(
                        itemIdentifier="AZswitch",
                        label="",
                        view=self._tbSwitch._nsObject,
                    )
                )
            self.w.addToolbar(
                PLUGIN_ID + ".toolbar",
                items,
                addStandardItems=False,
                displayMode="icon",
                toolbarStyle="unified",
            )
            # a visible title reserves the toolbar's leading edge, pushing items right
            try:
                nsw = self.w.getNSWindow()
                if nsw is not None:
                    nsw.setTitleVisibility_(1)  # NSWindowTitleHidden
            except Exception:
                pass
            self._floatToolbarBuilt = True
            self._syncFloatToolbar()
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _switchTooltip(self):
        if self._sidebarMode:
            return "Switch to Floating Window"
        return "Switch to Sidebar"

    @objc.python_method
    def _syncFloatToolbar(self):
        """Mirror current state into the toolbar controls."""
        try:
            preview = self.gP.preview
            self._tbMode.set(1 if preview._waterfall else 0)
            # view options are fixed in Waterfall
            try:
                self._tbView.enable(not preview._waterfall)
            except Exception:
                pass
            self._tbSwitch._nsObject.setToolTip_(self._switchTooltip())
        except Exception:
            pass

    def phoneQRClicked_(self, sender):
        """Start the LAN mirror server (once) and show its QR code."""
        try:
            url = self._startPhoneServer()
            if not url:
                return
            pop = Popover((180, 194))
            pop.img = ImageView((24, 24, 132, 132))
            qr = makeQRImage(url, 264.0)  # 2x the view for retina
            if qr is not None:
                pop.img.setImage(imageObject=qr)
            disp = url.rstrip("/")
            pop.url = Button((24, 162, 132, 18), disp, callback=self.phoneOpenURL_)
            link = pop.url._nsObject
            link.setBordered_(False)
            try:
                link.setFocusRingType_(1)  # NSFocusRingTypeNone
            except Exception:
                pass
            try:
                # scale the font so the URL spans the QR width
                refW = (
                    NSAttributedString.alloc()
                    .initWithString_attributes_(
                        disp, {NSFontAttributeName: NSFont.systemFontOfSize_(11.0)}
                    )
                    .size()
                    .width
                )
                fs = max(7.0, min(15.0, 11.0 * (132.0 / refW) if refW > 1 else 11.0))
                link.setAttributedTitle_(
                    NSAttributedString.alloc().initWithString_attributes_(
                        disp,
                        {
                            NSForegroundColorAttributeName: NSColor.linkColor(),
                            NSFontAttributeName: NSFont.systemFontOfSize_(fs),
                        },
                    )
                )
            except Exception:
                pass
            self._qrPop = pop  # retain: the popover dies without a live reference
            pop.open(parentView=self._tbPhone._nsObject, preferredEdge="bottom")
        except Exception:
            print(traceback.format_exc())

    def phoneOpenURL_(self, sender):
        """Open the LAN mirror URL in the Mac's default browser."""
        try:
            from AppKit import NSWorkspace
            from Foundation import NSURL

            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(self._phoneURL))
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _startPhoneServer(self):
        """Start (once) the LAN HTTP server for the remote page; return its URL."""
        if getattr(self, "_phoneServer", None) is not None:
            # LAN IP can change after sleep/wake; the 0.0.0.0 server keeps working
            self._phoneURL = f"http://{lanIP()}:{self._phoneServer.server_address[1]}/"
            return self._phoneURL
        # imported on first use: http.server adds ~15-50 ms to every Glyphs launch
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        plugin = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # keep the Macro console clean

            def do_GET(self):
                try:
                    plugin._phoneLastReq = time.time()
                    path = self.path.split("?")[0]
                    if path == "/":
                        body = REMOTE_PAGE_HTML.encode("utf-8")
                        ctype = "text/html; charset=utf-8"
                    elif path == "/seq":
                        body = (f"{plugin._phoneSeq}:{plugin._phoneViewMode}").encode(
                            "ascii"
                        )
                        ctype = "text/plain"
                    elif path == "/mode":
                        # remote-only view mode; the desktop panel is untouched
                        match = re.search(r"m=([01])", self.path)
                        if match is not None:
                            plugin._phoneViewMode = int(match.group(1))
                            plugin._phoneDirty = True
                        body = b"ok"
                        ctype = "text/plain"
                    elif path == "/state":
                        # prebuilt by the snapshot tick on the main thread
                        body = plugin._phoneStateJSON
                        ctype = "application/json"
                    elif path == "/set":
                        # remote-only overrides; the desktop panel is untouched
                        params = parse_qs(
                            self.path.partition("?")[2], keep_blank_values=True
                        )  # blank text= clears the override
                        if "ov" in params:
                            try:
                                plugin._phoneOvMul = max(
                                    1.0, min(4.0, float(params["ov"][0]))
                                )
                            except Exception:
                                pass
                        if "wf" in params:
                            try:
                                plugin._phoneWfMul = max(
                                    1.0, min(4.0, float(params["wf"][0]))
                                )
                            except Exception:
                                pass
                        if "text" in params:
                            plugin._phoneText = params["text"][0][:2000]
                        plugin._phoneDirty = True
                        body = b"ok"
                        ctype = "text/plain"
                    elif path == "/preview.pdf":
                        body = plugin._phonePdf or b""
                        if not body:
                            self.send_error(503)
                            return
                        ctype = "application/pdf"
                    elif path == "/preview.png":
                        # non-WebKit fallback; encoded only while requested
                        plugin._phonePngReq = time.time()
                        body = plugin._phonePng or b""
                        if not body:
                            plugin._phoneDirty = True  # next tick encodes one
                            self.send_error(503)
                            return
                        ctype = "image/png"
                    else:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    pass  # the device dropped the socket mid-write: routine

        try:
            server = ThreadingHTTPServer(("0.0.0.0", 0), _Handler)
        except Exception:
            print(traceback.format_exc())
            return None
        server.daemon_threads = True
        self._phoneServer = server
        self._phonePdf = None
        self._phonePng = None
        self._phonePngReq = 0.0
        self._phoneSeq = 0
        self._phoneDirty = True
        self._phoneSample = ""
        self._phoneSnapshotting = False
        self._phoneOvMul = 1.0  # Overview size multiplier (x1-x4)
        self._phoneWfMul = 1.0  # Waterfall ramp multiplier (x1-x4)
        self._phoneText = ""  # "" = follow the panel
        self._phoneStateJSON = b"{}"
        self._phoneViewMode = 1 if self.gP.preview._waterfall else 0
        self._phoneLastReq = time.time()
        self._phoneURL = f"http://{lanIP()}:{server.server_address[1]}/"
        # served only once its state exists: the handler reads it without defaults
        threading.Thread(target=server.serve_forever, daemon=True).start()
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            0.4, self, "phoneSnapshotTick:", None, True
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        return self._phoneURL

    @objc.python_method
    def _storePhoneCapture(self, doc):
        """Store a PDF of the whole preview (plus a PNG of the visible rect while one is
        being requested). None if nothing was captured, else whether it changed."""
        rect = doc.bounds()
        if rect.size.width < 1 or rect.size.height < 1:
            return None
        pdf = doc.dataWithPDFInsideRect_(rect)
        if pdf is None or not pdf.length():
            return None
        newPdf = bytes(pdf)
        changed = PDF_VOLATILE.sub(b"", newPdf) != PDF_VOLATILE.sub(
            b"", self._phonePdf or b""
        )
        self._phonePdf = newPdf
        if time.time() - self._phonePngReq < 5.0:
            vr = doc.visibleRect()
            rep = doc.bitmapImageRepForCachingDisplayInRect_(vr)
            if rep is not None:
                doc.cacheDisplayInRect_toBitmapImageRep_(vr, rep)
                data = rep.representationUsingType_properties_(4, {})  # 4 = PNG
                if data is not None:
                    png = bytes(data)
                    changed = changed or png != self._phonePng
                    self._phonePng = png
        return changed

    @objc.python_method
    def _phoneOverrideCapture(self, preview):
        """Capture the whole preview with the remote page's mode and overrides; the
        finally restores the desktop's before AppKit paints."""
        doc = preview.docView
        curMode = 1 if preview._waterfall else 0
        effMode = self._phoneViewMode
        ovMul = self._phoneOvMul
        savedWater = preview._waterfall
        savedSize, savedCols = preview._size, preview._columns
        savedLadder = preview._waterfallLadder
        savedGlyphs = list(preview._glyphsList)
        savedScrollPt = None  # relayout resizes the live docView; restore scroll
        try:
            _sv0 = doc.enclosingScrollView()
            if _sv0 is not None:
                savedScrollPt = _sv0.contentView().bounds().origin
        except Exception:
            pass
        overridden = False
        try:
            if effMode != curMode:
                preview._waterfall = effMode == 1
                overridden = True
            if effMode == 0:
                if int(savedCols) != 0:
                    preview._columns = 0  # remote Overview is always Auto columns
                    overridden = True
                if abs(ovMul - 1.0) > 0.01:
                    preview._size = max(4.0, float(savedSize) * ovMul)
                    overridden = True
            elif effMode == 1:
                mul = self._phoneWfMul
                if abs(mul - 1.0) > 0.01:
                    preview._waterfallLadder = [max(4.0, s * mul) for s in savedLadder]
                    overridden = True
            # path caches key on the list (LRU-bounded), so swapping needs no bust
            phoneText = self._phoneText
            if phoneText:
                names = self.makeList(phoneText)
                if names and names != preview._glyphsList:
                    preview._glyphsList = names
                    overridden = True
            if overridden:
                preview.updateContentSize()
            return self._storePhoneCapture(doc)
        finally:
            preview._waterfall = savedWater
            preview._size, preview._columns = savedSize, savedCols
            preview._waterfallLadder = savedLadder
            preview._glyphsList = savedGlyphs
            if overridden:
                preview.updateContentSize()
            if savedScrollPt is not None:
                try:
                    _sv1 = doc.enclosingScrollView()
                    if _sv1 is not None:
                        _clip1 = _sv1.contentView()
                        _clip1.scrollToPoint_(savedScrollPt)
                        _sv1.reflectScrolledClipView_(_clip1)
                except Exception:
                    pass

    def phoneSnapshotTick_(self, timer):
        """Main-thread timer: re-capture the preview for the remote page, with its
        own mode and overrides applied only to the offscreen capture."""
        try:
            if time.time() - self._phoneLastReq > 5.0:
                return
            if not self._closed and not self._phoneDirty and self._phoneSeq:
                return
            preview = self.gP.preview
            doc = preview.docView
            # captures re-run drawRect_, which would re-flag dirty and loop forever
            self._phoneSnapshotting = True
            try:
                changed = self._phoneOverrideCapture(preview)
                # repaint what the overrides disturbed while the guard is still up
                doc.displayIfNeeded()
            finally:
                self._phoneSnapshotting = False
            try:
                self._phoneSample = str(self.gS.edit.get() or "")
            except Exception:
                pass
            self._phoneStateJSON = json.dumps(
                {
                    "mode": int(self._phoneViewMode),
                    "ovmul": self._phoneOvMul,
                    "wfmul": self._phoneWfMul,
                    "text": str(self._phoneText or self._phoneSample or ""),
                }
            ).encode("utf-8")
            if changed is None:
                return  # nothing captured: stay dirty and retry next tick
            if changed:
                self._phoneSeq += 1
            self._phoneDirty = False
        except Exception:
            pass

    @objc.python_method
    def _intDefault(self, key, fallback, lo, hi):
        value = Glyphs.defaults[PLUGIN_ID + key]
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = fallback
        return max(lo, min(hi, value))

    def textChanged_(self, sender):
        text = self.gS.edit.get()
        Glyphs.defaults[PLUGIN_ID + ".text"] = text
        if not text:
            # empty field: fall back to mirroring the current Edit tab
            self._lastTabList = None
            self.syncFromTab_(None)
            return
        self.gP.preview.setGlyphs(self.makeList(text))
        self.gP.preview.updateContentSize()

    @objc.python_method
    def currentAppIsDark(self):
        try:
            from AppKit import (
                NSApplication,
                NSAppearanceNameDarkAqua,
                NSAppearanceNameAqua,
            )

            ap = NSApplication.sharedApplication().effectiveAppearance()
            match = ap.bestMatchFromAppearancesWithNames_(
                [NSAppearanceNameAqua, NSAppearanceNameDarkAqua]
            )
            return match == NSAppearanceNameDarkAqua
        except Exception:
            return False

    @objc.python_method
    def applyTheme(self):
        """Match preview colours to light/dark mode (re-run via appearanceCallback)."""
        dark = self.currentAppIsDark()
        self._darkMode = dark
        if dark:
            fore = NSColor.whiteColor()
            back = NSColor.colorWithCalibratedWhite_alpha_(0.08, 1.0)
        else:
            fore = NSColor.blackColor()
            # #E4E4E4, not white, so cards read as surfaces
            back = NSColor.colorWithCalibratedWhite_alpha_(228.0 / 255.0, 1.0)
        self.gP.preview._foreColour = fore
        self.gP.preview.setBackgroundColour(back)
        self.gP.preview.redraw()

    @objc.python_method
    def _setPreviewCard(self, on):
        """Narrow hosts: preview in its own card. The scroll view paints opaque, so it
        is inset 1pt and corner-clipped to keep the card rim visible."""
        on = bool(on)
        if getattr(self, "_previewCardOn", None) is on:
            return  # called often; touch frames only on a real change
        self._previewCardOn = on
        pcard = self._previewCard
        if pcard is not None:
            pcard.setHidden_(not on)
            if on:
                pcard.setNeedsDisplay_(True)
        try:
            inset = AZ_SIDE_MARGIN + 1.0
            topInset = _CARD_FACING_INSET + 1.0
            if on:
                self.gP.preview.setPosSize((inset, topInset, -inset, -inset))
            else:
                self.gP.preview.setPosSize((0, 0, -0, -0))
            sv = self.gP.preview._nsObject
            sv.setWantsLayer_(True)
            layer = sv.layer()
            if layer is not None:
                layer.setCornerRadius_(max(0.0, AZ_CARD_RADIUS - 1.0) if on else 0.0)
                layer.setMasksToBounds_(on)
        except Exception:
            pass

    @objc.python_method
    def _layoutSidebar(self):
        """Lay out the settings band; a floating window gets _layoutToolbar instead."""
        # margin + card padding: lines up with card content in the other AZ tabs
        sp, lblW, edY = AZ_SIDE_MARGIN + AZ_CARD_PAD_X, 34, 22
        try:
            # top of the text-field row
            rowTTop = AZ_SIDE_MARGIN + AZ_CARD_PAD_Y + _CARD_PAD_Y_EXTRA
            try:
                splitNS = self.w.split.getNSSplitView()
            except Exception:
                splitNS = None
            # floating: horizontal toolbar; G4 sidebar tab: vertical band
            card = self._settingsCard
            if splitNS is not None and not self._narrowHosted():
                if card is not None:
                    card.setHidden_(True)  # floating: thin toolbar, no card
                self._setPreviewCard(False)
                self._layoutToolbar(splitNS)
                return
            if card is not None:
                card.setHidden_(False)
                card.setNeedsDisplay_(True)
            self._setPreviewCard(True)
            if splitNS is not None:
                try:
                    # before placing rows: negative posSizes read the pane height
                    self._applySettingsDivider(splitNS)
                except Exception:
                    pass
            embedded = self._narrowHosted()
            self.gP.preview._embedded = embedded
            # Waterfall: one instance on a size ladder, so no Size or Columns
            waterfall = bool(self.gP.preview._waterfall)
            for ctl in (self.gS.sizeLabel, self.gS.sizeSlider, self.gS.sizeText):
                ctl.show(not waterfall)
            # narrow hosts are a single column
            for ctl in (
                self.gS.columnsLabel,
                self.gS.columnsText,
                self.gS.columnsStepper,
            ):
                ctl.show(not waterfall and not embedded)
            self.gS.activeCheckBox.show(False)  # in the context menu
            for ctl in self._waterfallScaleControls():  # floating toolbar only
                ctl.show(False)
            self.gS.waterfallMultiColumns.show(False)
            self.gS.embedMode.show(embedded)
            self.gS.modeSeg.show(False)  # in the context menu
            if embedded:
                self.gS.embedMode.set(1 if waterfall else 0)
            # gated on mode, not host: Waterfall needs the picker when docked too
            self.gS.focusPopup.show(waterfall)
            # rows stack off a running `y` with one gap, so nothing drifts
            rowGap = 6  # the 22pt row bands already add ~3pt air each side
            # Centre controls in an edY-high band, don't resize: bezels keep their
            # intrinsic height (small segmented: ~19.5pt whatever the frame).
            rowH = edY

            def _rowY(top, ctlH):
                return top + max(0.0, (rowH - ctlH) / 2.0)

            # segmented bezel overshoots ~2pt left; this aligns it with the field
            segX = sp + 2
            y = rowTTop + edY + rowGap
            if embedded:
                self.gS.embedMode.setPosSize((segX, _rowY(y, 16), -sp, 16))
                y += rowH + rowGap
            if waterfall:
                # NSPopUpButton bezel paints ~3pt past each side; match the field
                self.gS.focusPopup.setPosSize(
                    (sp + POPUP_BEZEL, _rowY(y, 20), -(sp + POPUP_BEZEL), 20)
                )
                y += rowH + rowGap
            if not waterfall:
                y += 4
                self.gS.sizeLabel.setPosSize((sp, y + 1, lblW, 14))
                self.gS.sizeSlider.setPosSize((sp + lblW + 2, y, -(sp + 46), 16))
                self.gS.sizeText.setPosSize((-(sp + 34), y - 2, 34, 19))  # fits "160"
                y += 17 + rowGap  # the 19-high value field bottoms at y+17
                if not embedded:
                    self.gS.columnsLabel.setPosSize((sp, y, 22, 20))
                    self.gS.columnsText.setPosSize((sp + 26, y, 40, 21))
                    self.gS.columnsStepper.setPosSize(
                        (sp + 66, y, 19, 22)
                    )  # stuck to the field on purpose
                    y += 22 + rowGap
            contentBottom = max(rowTTop + edY, y - rowGap)
            # pin the band to the content height; re-pin only on change (runs often)
            if splitNS is not None:
                try:
                    newH = (
                        contentBottom
                        + AZ_CARD_PAD_Y
                        + _CARD_PAD_Y_EXTRA
                        + _CARD_FACING_INSET
                    )
                    if abs(newH - self._stackedTopH) > 0.5:
                        self._stackedTopH = newH
                        self._applySettingsDivider(splitNS)
                except Exception:
                    pass
            self._placeTextRow(rowTTop, sp, edY)
            try:
                self.gP.preview.updateContentSize()
            except Exception:
                pass
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _layoutToolbar(self, splitNS):
        """Floating window: settings as a thin bar (TOOLBAR_H) above the preview."""
        self._stackedTopH = TOOLBAR_H
        frame = self.gS._nsObject.frame()
        gh, gw = frame.size.height, frame.size.width
        if gw <= 1:
            return
        if abs(gh - TOOLBAR_H) > 1.0:
            try:
                self._applySettingsDivider(splitNS)
            except Exception:
                pass
            frame = self.gS._nsObject.frame()
            gh, gw = frame.size.height, frame.size.width
        if gh <= 1 or gw <= 1:
            return
        preview = self.gP.preview
        # _layoutSidebar returns before its own assignment on this path
        preview._embedded = False
        waterfall = bool(preview._waterfall)
        sp, edY = 12, 22

        def cy(h):
            return max(0.0, (gh - h) / 2.0)  # vertical centre (bottom-origin NSView)

        def place(ctl, x, w, h, mask):
            try:
                ctl._nsObject.setFrame_(NSMakeRect(x, cy(h), w, h))
                ctl._nsObject.setAutoresizingMask_(mask)
            except Exception:
                pass

        STICK_L = NSViewMaxXMargin | NSViewMinYMargin | NSViewMaxYMargin
        STICK_R = NSViewMinXMargin | NSViewMinYMargin | NSViewMaxYMargin
        FIELD_MAX = 200.0
        # Waterfall: "Start" and "Steps" label + field + arrows, then "Multi columns"
        WF_LABELS = (30.0, 38.0)
        WF_W = sum(WF_LABELS) + 2 * (2 + 34 + 2 + 13) + 10
        MULTI_W = 100.0

        masters = bool(preview._showMasters)
        instances = not masters
        # Left: [picker] field, mode cluster; right: Masters/Instances, Active only.
        # The mode segmented and sidebar toggle live in the native title-bar toolbar.
        showModeSeg = not waterfall  # masters vs instances is moot in Waterfall
        showActive = instances and not waterfall
        showSize = not waterfall
        showCols = showSize
        showWf = waterfall

        for _c in (self.gS.embedMode, self.gS.sidebarToggle):
            try:
                _c.show(False)
            except Exception:
                pass
        for _c in (self.gS.sizeLabel, self.gS.sizeSlider, self.gS.sizeText):
            try:
                _c.show(showSize)
            except Exception:
                pass
        for _c in (self.gS.columnsLabel, self.gS.columnsText, self.gS.columnsStepper):
            try:
                _c.show(showCols)
            except Exception:
                pass
        self.gS.modeSeg.show(showModeSeg)
        self.gS.activeCheckBox.show(showActive)
        self.gS.focusPopup.show(showWf)
        multiCols = self.gS.waterfallMultiColumns
        for _c in self._waterfallScaleControls() + (multiCols,):
            try:
                _c.show(showWf)
            except Exception:
                pass

        xr = gw - sp
        if showModeSeg:
            try:
                self.gS.modeSeg.set(0 if masters else 1)
            except Exception:
                pass
            MI_W = 160.0
            place(self.gS.modeSeg, xr - MI_W, MI_W, 18, STICK_R)
            xr -= MI_W + 8
        if showActive:
            try:
                self.gS.activeCheckBox.set(bool(preview._activeOnly))
            except Exception:
                pass
            AC_W = 92.0
            place(self.gS.activeCheckBox, xr - AC_W, AC_W, 16, STICK_R)
            xr -= AC_W + 10
        rightEdge = xr

        if showSize:
            clusterW = (28 + 4 + 90 + 4 + 34) + 12 + (20 + 2 + 50 + 2 + 13)
        else:
            clusterW = WF_W + 8 + MULTI_W + 10
        gapF = 14
        pickLead = 0.0
        if showWf:
            FP_W = 160.0
            place(self.gS.focusPopup, sp, FP_W, 20, STICK_L)
            pickLead = FP_W + 10.0
        fieldW = max(
            60.0, min(FIELD_MAX, (rightEdge - sp - pickLead) - gapF - clusterW - 10.0)
        )
        place(self.gS.edit, sp + pickLead, fieldW, edY, STICK_L)
        x = sp + pickLead + fieldW + gapF
        if showSize:
            place(self.gS.sizeLabel, x, 28, 14, STICK_L)
            x += 28 + 4
            place(self.gS.sizeSlider, x, 90, 16, STICK_L)
            x += 90 + 4
            place(self.gS.sizeText, x, 34, 19, STICK_L)
            x += 34 + 12
        if showCols:
            place(self.gS.columnsLabel, x, 20, 20, STICK_L)
            x += 20 + 2
            place(self.gS.columnsText, x, 50, 21, STICK_L)  # wider so "Auto" fits
            x += 50 + 2
            place(self.gS.columnsStepper, x, 13, 19, STICK_L)  # smaller arrows
        elif showWf:
            gS = self.gS
            for (label, field, arrows), labelW in zip(
                (
                    (
                        gS.waterfallStartLabel,
                        gS.waterfallStart,
                        gS.waterfallStartStepper,
                    ),
                    (
                        gS.waterfallStepsLabel,
                        gS.waterfallSteps,
                        gS.waterfallStepsStepper,
                    ),
                ),
                WF_LABELS,
            ):
                place(label, x, labelW, 14, STICK_L)
                x += labelW + 2
                place(field, x, 34, 19, STICK_L)
                x += 34 + 2
                place(arrows, x, 13, 19, STICK_L)
                x += 13 + 10
            place(multiCols, x - 2, MULTI_W, 18, STICK_L)

        self._stripFocusRings(self.gS._nsObject)
        self._syncFloatToolbar()

    @objc.python_method
    def _placeTextRow(self, rowTTop, sp, edY):
        """Place the sample field + sidebar toggle with frames and autoresizing masks:
        vanilla's negative-y posSize did not reflow here."""
        try:
            frame = self.gS._nsObject.frame()
            gh, gw = frame.size.height, frame.size.width
            if gh <= 1 or gw <= 1:
                return  # not laid out yet; re-runs on open / resize
            editY = gh - rowTTop - edY  # gS is not flipped
            vMask = NSViewMinYMargin  # stick to the top
            editX = sp
            fieldRight = gw - sp
            toggle = self.gS.sidebarToggle
            toggle.show(self._sidebarCapable)
            if self._sidebarCapable:
                toggleX = gw - sp - TOGGLE_BTN_W
                # bezel overshoots ~1pt, the field insets ~0.5pt: shrink to match
                btnPad = 5.0
                toggle._nsObject.setFrame_(
                    NSMakeRect(
                        toggleX, editY + btnPad / 2.0, TOGGLE_BTN_W, edY - btnPad
                    )
                )
                toggle._nsObject.setAutoresizingMask_(NSViewMinXMargin | vMask)
                toggle._nsObject.setToolTip_(self._switchTooltip())
                fieldRight = toggleX - 6
            editW = max(1.0, fieldRight - editX)
            self.gS.edit._nsObject.setFrame_(NSMakeRect(editX, editY, editW, edY))
            self.gS.edit._nsObject.setAutoresizingMask_(NSViewWidthSizable | vMask)
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _stackedBandH(self):
        return float(self._stackedTopH)

    @objc.python_method
    def _editWindow(self):
        """The active font's Edit window, or None."""
        try:
            font = Glyphs.font
            if font is None:
                return None
            doc = font.parent
            try:
                wc = doc.windowController()
                if wc is not None and wc.window() is not None:
                    return wc.window()
            except Exception:
                pass
            try:
                wcs = doc.windowControllers()
                if wcs is not None and len(wcs) > 0:
                    return wcs[0].window()
            except Exception:
                pass
        except Exception:
            pass
        return None

    @objc.python_method
    def _applySettingsDivider(self, splitNS):
        """Place divider 0, flipping it if NSSplitView measured from the far side.
        Pins the pane bounds first: the split delegate clamps the divider to them."""
        self._setSettingsPaneBounds()
        pos = self._stackedBandH()
        splitNS.setPosition_ofDividerAtIndex_(pos, 0)
        splitNS.adjustSubviews()
        try:
            if abs(self.gS._nsObject.frame().size.height - pos) > 40:
                total = splitNS.frame().size.height
                splitNS.setPosition_ofDividerAtIndex_(max(40.0, total - pos), 0)
                splitNS.adjustSubviews()
        except Exception:
            pass

    @objc.python_method
    def _setSettingsPaneBounds(self):
        """Pin the settings pane's height in vanilla's paneDescription; the split's
        delegate clamps the divider to minSize/maxSize."""
        try:
            panes = self.w.split._paneDescriptions
        except Exception:
            return
        desc = None
        for pane in panes:
            if pane.get("identifier") == "settings":
                desc = pane
                break
        if desc is None:
            return
        # fixed: a dragged or stale band height left the controls overlapping
        size = self._stackedBandH()
        desc["minSize"] = desc["maxSize"] = desc["size"] = size
        desc["fixedSize"] = True
        desc["resizeFlexibility"] = False

    @objc.python_method
    def _relayoutSplit(self, splitNS):
        """Re-pin the settings band and re-lay out after the split changed host."""
        try:
            self._setSettingsPaneBounds()
            splitNS.adjustSubviews()
            self._applySettingsDivider(splitNS)
            splitNS.display()
            self._layoutSidebar()
        except Exception:
            print(traceback.format_exc())

    def toggleSidebarFloat_(self, sender):
        # sidebar band button and toolbar switch: G4 left sidebar <-> floating window
        try:
            if bool(self._sidebarMode):
                self.floatFromSidebar_(None)
                return
            self._setEditSidebarCollapsed(False)
            cont = self._sidebarTabContainer
            if self._visibleSidebarContainer() is not None:
                self.adoptSidebarHost(cont)
            elif cont is not None and cont.window() is not None:
                # the expand un-hides the container a runloop turn later
                self.performSelector_withObject_afterDelay_(
                    b"readoptSidebar:", None, 0.0
                )
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _setEditSidebarCollapsed(self, collapse):
        """Collapse/expand the G4 Edit window's native left sidebar. No-op on G3."""
        try:
            ctrl = getattr(self, "_editSidebarCtrl", None)
            svc = None
            if ctrl is not None and ctrl.respondsToSelector_(
                b"mainSplitViewController"
            ):
                svc = ctrl.mainSplitViewController()
            if svc is None:
                tab = Glyphs.font.currentTab if Glyphs.font else None
                if tab is not None and tab.respondsToSelector_(
                    b"mainSplitViewController"
                ):
                    svc = tab.mainSplitViewController()
            if svc is None:
                return
            for item in svc.splitViewItems() or []:
                try:
                    if int(item.behavior()) == 1:  # NSSplitViewItemBehaviorSidebar
                        if bool(item.isCollapsed()) != bool(collapse):
                            item.setCollapsed_(bool(collapse))
                            try:
                                svc.view().layoutSubtreeIfNeeded()  # settle now
                            except Exception:
                                pass
                        return
                except Exception:
                    pass
        except Exception:
            pass

    def floatFromSidebar_(self, sender):
        try:
            host = self._sidebarHost
            if host is not None:
                self.releaseSidebarHost(host)
            self.w.show()
            nsw = self.w.getNSWindow()
            if nsw is not None:
                # rebuild if the first build failed; re-show if the user hid it
                if nsw.toolbar() is None:
                    self._floatToolbarBuilt = False
                    self._buildFloatToolbar()
                tb = nsw.toolbar()
                if tb is not None:
                    tb.setVisible_(True)
                nsw.makeKeyAndOrderFront_(None)
            self._layoutSidebar()
            self._syncFloatToolbar()
            self._setEditSidebarCollapsed(True)  # so no empty sidebar tab lingers
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _homeSplitInFloating(self, splitNS):
        """Put the split back into the floating window's content view."""
        try:
            nsw = self.w.getNSWindow()
            if nsw is None:
                return
            content = nsw.contentView()
            splitNS.setFrame_(content.bounds())
            splitNS.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            content.addSubview_(splitNS)
            self._relayoutSplit(splitNS)
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def adoptSidebarHost(self, container):
        """Move the panel UI into a G4 left-sidebar tab (one host at a time: the floating
        shell is hidden)."""
        try:
            if container is None:
                return
            # outlives release: windowClosed_ hands the panel back to a visible tab
            self._sidebarTabContainer = container
            if self.w is None or self._closed:
                self.showWindow_(None)  # build the UI
            if self.w is None:
                return
            if self._hidden:
                self._wake()
            splitNS = self.w.split.getNSSplitView()
            if splitNS.superview() is container:
                self._sidebarHost = container
                self._sidebarMode = True
                self._setSidebarPlaceholderHidden(container, True)
                return
            self._splitHome = (
                splitNS.superview(),
                splitNS.frame(),
                splitNS.autoresizingMask(),
            )
            splitNS.removeFromSuperview()
            container.addSubview_(splitNS)
            splitNS.setFrame_(container.bounds())
            splitNS.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            self._sidebarHost = container
            self._sidebarMode = True
            self._setSidebarPlaceholderHidden(container, True)
            try:
                self.w.getNSWindow().orderOut_(None)
            except Exception:
                pass
            # narrow host: stacked settings band (also runs _layoutSidebar)
            self._relayoutSplit(splitNS)
            self.gP.preview.updateContentSize()
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def releaseSidebarHost(self, container):
        """Sidebar tab went away: reparent the split into the hidden floating shell,
        deliberately not re-shown (the Window menu brings it back)."""
        try:
            if self._sidebarHost is not container:
                return  # another tab's controller, or already re-adopted
            self._sidebarHost = None
            self._sidebarMode = False
            self._setSidebarPlaceholderHidden(container, False)
            splitNS = self.w.split.getNSSplitView()
            home = self._splitHome
            self._splitHome = None
            if home and home[0] is not None:
                splitNS.removeFromSuperview()
                home[0].addSubview_(splitNS)
                splitNS.setFrame_(home[1])
                splitNS.setAutoresizingMask_(home[2])
            self._relayoutSplit(splitNS)
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _setSidebarPlaceholderHidden(self, container, hidden):
        """Toggle the placeholder built in AZSPSidebarController.loadView."""
        try:
            for sub in container.subviews():
                if sub.identifier() == "AZSPSidebarPlaceholder":
                    sub.setHidden_(hidden)
                    return
        except Exception:
            pass

    @objc.python_method
    def _stripFocusRings(self, root):
        """Remove focus rings from every view under root (1 = NSFocusRingTypeNone)."""
        views = [root] if root is not None else []
        while views:
            view = views.pop()
            try:
                view.setFocusRingType_(1)
            except Exception:
                pass
            try:
                views.extend(list(view.subviews()))
            except Exception:
                pass

    @objc.python_method
    def _narrowHosted(self):
        """True in a narrow one-column host (the G4 sidebar tab)."""
        return bool(self._sidebarMode)

    @objc.python_method
    def applySize(self, value, echoText=True):
        value = max(30, min(160, int(round(value))))
        Glyphs.defaults[PLUGIN_ID + ".size"] = value
        self.gP.preview._size = value
        try:
            self.gS.sizeSlider.set(value)
            # no echo while typing: writing back the clamped value blocks editing
            if echoText:
                self.gS.sizeText.set(str(value))
        except Exception:
            pass
        # Size rescales the cards only; the panel keeps the width the user dragged
        self.gP.preview.updateContentSize()
        return value

    @objc.python_method
    def _sizeFieldArrowStep(self):
        """Up/Down in the Size field steps the value (Shift ±10); True if consumed.
        Steps from the field's text, so it works mid-typing."""
        try:
            app = objc.lookUpClass("NSApplication").sharedApplication()
            ev = app.currentEvent()
            if ev is None:
                return False
            kc = ev.keyCode()
            if kc not in (125, 126):  # 126 = Up, 125 = Down
                return False
            shift = bool(int(ev.modifierFlags()) & NSEventModifierFlagShift)
            step = 10 if shift else 1
            delta = step if kc == 126 else -step
            try:
                cur = int(round(float(self.gS.sizeText.get())))
            except (TypeError, ValueError):
                cur = int(self.gP.preview._size)
            newVal = self.applySize(cur + delta, echoText=True)
            # the live field editor overrides setStringValue_, so write to it directly
            try:
                editor = self.gS.sizeText._nsObject.currentEditor()
                if editor is not None:
                    editor.setString_(str(int(newVal)))
                    editor.selectAll_(None)
            except Exception:
                pass
            return True
        except Exception:
            return False

    def sizeSliderChanged_(self, sender):
        self.applySize(sender.get())

    def sizeTextChanged_(self, sender):
        try:
            value = float(sender.get())
        except (TypeError, ValueError):
            return
        if value < 30:
            return  # likely mid-typing ("5" heading for "56"): don't clamp yet
        self.applySize(value, echoText=False)

    @objc.python_method
    def applyColumns(self, columns):
        columns = max(0, min(10, int(columns)))
        Glyphs.defaults[PLUGIN_ID + ".columns"] = columns
        self.gP.preview._columns = columns
        self.gP.preview.updateContentSize()
        return columns

    def windowResized_(self, sender):
        # the window size is the user's: re-flow content + toolbar, never refit it
        preview = getattr(getattr(self, "gP", None), "preview", None)
        if preview is None:
            return
        if self._narrowHosted():
            return  # sidebar: the host drives the geometry
        self._layoutSidebar()
        try:
            preview.updateContentSize()
        except Exception:
            print(traceback.format_exc())

    def columnsStepperChanged_(self, sender):
        columns = self.applyColumns(int(round(sender.get())))
        self.gS.columnsText.set("Auto" if columns == 0 else str(columns))

    def modeChanged_(self, sender):
        self._setShowMasters(sender.get() == 0)  # segment 0 = Masters

    def modeSegMenu_(self, sender):
        masters = sender.selectedSegment() == 0
        self._setShowMasters(masters)
        # the menu stays open: add/remove the Instances-only "Active only" row live
        try:
            segItem = sender.enclosingMenuItem()
            menu = segItem.menu() if segItem is not None else None
            if menu is not None:
                existing = menu.itemWithTag_(ACTIVE_ONLY_MENU_TAG)
                if not masters and existing is None:
                    menu.insertItem_atIndex_(
                        self._makeActiveOnlyItem(), menu.indexOfItem_(segItem) + 1
                    )
                elif masters and existing is not None:
                    menu.removeItem_(existing)
        except Exception:
            print(traceback.format_exc())

    def activeOnlyCheck_(self, sender):
        self._setActiveOnly(bool(sender.state()))

    @objc.python_method
    def _makeActiveOnlyItem(self):
        """Menu row with the 'Active instances only' checkbox, tagged for removal."""
        preview = self.gP.preview
        rowW, rowH = 210.0, 22.0
        row = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, rowW, rowH))
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(14.0, 1.0, rowW - 18.0, 20.0))
        btn.setButtonType_(3)  # NSButtonTypeSwitch (checkbox)
        btn.setTitle_("Active instances only")
        btn.setFont_(NSFont.menuFontOfSize_(13.0))
        btn.setState_(1 if bool(preview._activeOnly) else 0)
        btn.setTarget_(self)
        btn.setAction_("activeOnlyCheck:")
        row.addSubview_(btn)
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Active instances only", None, ""
        )
        item.setView_(row)
        item.setTag_(ACTIVE_ONLY_MENU_TAG)
        return item

    @objc.python_method
    def _addModeSegmentItem(self, menu):
        """Masters/Instances segmented control for the contextual menu (an NSPopUpButton
        can't open inside a menu's tracking loop)."""
        from AppKit import NSSegmentedControl, NSSegmentStyleRounded

        preview = self.gP.preview
        # taller row, control at the bottom: top padding for the menu's first item
        rowW, rowH = 210.0, 32.0
        row = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, rowW, rowH))
        seg = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect(14.0, 2.0, rowW - 14.0 - 8.0, 22.0)
        )
        seg.setSegmentCount_(2)
        seg.setLabel_forSegment_("Masters", 0)
        seg.setLabel_forSegment_("Instances", 1)
        try:
            seg.setSegmentStyle_(NSSegmentStyleRounded)
            seg.setSegmentDistribution_(1)  # NSSegmentDistributionFillEqually (10.13+)
        except Exception:
            pass
        seg.setFont_(NSFont.menuFontOfSize_(13.0))
        seg.setSelectedSegment_(0 if bool(preview._showMasters) else 1)
        seg.setTarget_(self)
        seg.setAction_("modeSegMenu:")
        row.addSubview_(seg)
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
        item.setView_(row)
        menu.addItem_(item)

    @objc.python_method
    def _setShowMasters(self, on):
        on = bool(on)
        Glyphs.defaults[PLUGIN_ID + ".showMasters"] = on
        self.gP.preview._showMasters = on
        try:
            self.gS.modeSeg.set(0 if on else 1)
        except Exception:
            pass
        self._refreshFocusPicker()
        self._layoutSidebar()
        self.gP.preview.updateContentSize()

    @objc.python_method
    def _setViewOption(self, attr, on):
        """Apply and persist one VIEW_OPTIONS toggle."""
        preview = self.gP.preview
        Glyphs.defaults[PLUGIN_ID + "." + attr[1:]] = on
        setattr(preview, attr, on)
        if attr in ("_showGuides", "_showOutlines"):
            self._syncDependentToggles()
        if attr == "_showSpacing":
            preview.updateContentSize()  # a line under each glyph: rows get taller
        else:
            preview.redraw()

    @objc.python_method
    def _syncDependentToggles(self):
        """Popover: enable sub-toggles only while their parent is on (nodes/anchors need
        outlines, zones need metrics)."""
        pop = getattr(self, "_viewPopover", None)
        if pop is None:
            return
        try:
            for attr, _, _ in VIEW_OPTIONS:
                getattr(pop, attr[5:].lower()).enable(self._viewOptionEnabled(attr))
        except Exception:
            pass

    @objc.python_method
    def _viewOptionEnabled(self, attr):
        """View options apply to Overview only; zones need metrics, nodes and anchors
        need outlines."""
        preview = self.gP.preview
        if preview._waterfall:
            return False
        if attr == "_showZones":
            return bool(preview._showGuides)
        if attr in ("_showNodes", "_showAnchors"):
            return bool(preview._showOutlines)
        return True

    def dispModeChanged_(self, sender):
        """Segmented Overview / Waterfall mode switch."""
        try:
            idx = int(sender.get())
            # sync both mode controls (floating toolbar, narrow-host band)
            for _seg in (self.gS.embedMode, self._tbMode):
                try:
                    if _seg.get() != idx:
                        _seg.set(idx)
                except Exception:
                    pass
            preview = self.gP.preview
            water = idx == 1  # segmented order: 0=Overview 1=Waterfall
            preview._waterfall = water
            Glyphs.defaults[PLUGIN_ID + ".waterfall"] = water
            self._refreshFocusPicker()
            self._layoutSidebar()
            self._applyMinHeight()
            preview.updateContentSize()
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _applyMinHeight(self):
        """Set the floating window's min height, growing it if shorter."""
        try:
            if self._narrowHosted():
                return
            minH = CONTENT_MIN_H
            nsw = self.w.getNSWindow()
            if nsw is None:
                return
            nsw.setContentMinSize_(NSMakeSize(560.0, float(minH)))
            content = nsw.contentRectForFrameRect_(nsw.frame())
            if content.size.height < minH - 0.5:
                grow = minH - content.size.height
                fr = nsw.frame()
                nsw.setFrame_display_animate_(
                    NSMakeRect(
                        fr.origin.x,
                        fr.origin.y - grow,
                        fr.size.width,
                        fr.size.height + grow,
                    ),
                    True,
                    False,
                )
        except Exception:
            pass

    # Waterfall instance picker; row 0 = "Current master"
    def focusPickerChanged_(self, sender):
        keys = getattr(self, "_focusKeys", [])
        idx = sender.get()
        k = idx - 1
        if 0 <= k < len(keys):
            self.gP.preview._focusKey = keys[k]
            Glyphs.defaults[PLUGIN_ID + ".focusKey"] = keys[k]
        elif idx == 0:
            self.gP.preview._focusKey = (
                ""  # no instance: the ramp follows the Edit view's master
            )
            Glyphs.defaults[PLUGIN_ID + ".focusKey"] = ""
        self.gP.preview.updateContentSize()

    @objc.python_method
    def stepFocusItem(self, delta):
        """Step the Waterfall picker by delta; False lets the key fall through."""
        try:
            preview = self.gP.preview
            if not preview._waterfall:
                return False
            keys = getattr(self, "_focusKeys", [])
            if not keys:
                return False
            cur = preview._focusKey
            i = (keys.index(cur) + 1) if cur in keys else 0
            i = (i + delta) % (len(keys) + 1)
            self.gS.focusPopup.set(i)
            self.focusPickerChanged_(self.gS.focusPopup)
            return True
        except Exception:
            print(traceback.format_exc())
            return False

    def waterfallCustom_(self, sender):
        """Right-click "Waterfall Sizes…" (sidebar): Start and Steps in an alert."""
        try:
            gS = self.gS
            TextField = objc.lookUpClass("NSTextField")
            box = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 160, 54))
            fields = []
            for row, (title, value) in enumerate(
                (
                    ("Start (pt)", gS.waterfallStartStepper.get()),
                    ("Steps", gS.waterfallStepsStepper.get()),
                )
            ):
                y = 32.0 - row * 30.0
                label = TextField.labelWithString_(title)
                label.setFrame_(NSMakeRect(0, y + 2, 70, 18))
                field = TextField.alloc().initWithFrame_(NSMakeRect(76, y, 60, 22))
                field.setStringValue_(str(int(value)))
                box.addSubview_(label)
                box.addSubview_(field)
                fields.append(field)
            fields[0].setNextKeyView_(fields[1])
            alert = objc.lookUpClass("NSAlert").alloc().init()
            alert.setMessageText_("Waterfall Sizes")
            alert.setInformativeText_(
                "The smallest size and how many sizes; each is about 13% larger than "
                "the one before."
            )
            alert.addButtonWithTitle_("OK")
            alert.addButtonWithTitle_("Cancel")
            alert.setAccessoryView_(box)
            alert.window().setInitialFirstResponder_(fields[0])
            if alert.runModal() != 1000:  # not OK
                return
            values = [_finiteFloat(field.stringValue()) for field in fields]
            self._applyWaterfallScale(start=values[0], steps=values[1])
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _waterfallScaleControls(self):
        gS = self.gS
        return (
            gS.waterfallStartLabel,
            gS.waterfallStart,
            gS.waterfallStartStepper,
            gS.waterfallStepsLabel,
            gS.waterfallSteps,
            gS.waterfallStepsStepper,
        )

    @objc.python_method
    def _applyWaterfallScale(self, start=None, steps=None, echo=True):
        """Rebuild the ladder from Start / Steps (None keeps the current one) and save
        both. The arrows hold the current values. Typing passes echo=False: rewriting
        a field mid-edit blocks typing (see applySize)."""
        gS = self.gS
        if start is None:
            start = gS.waterfallStartStepper.get()
        if steps is None:
            steps = gS.waterfallStepsStepper.get()
        ladder = waterfallSizes(start, steps)
        start, steps = ladder[0], len(ladder)
        gS.waterfallStartStepper.set(start)
        gS.waterfallStepsStepper.set(steps)
        if echo:
            gS.waterfallStart.set(str(start))
            gS.waterfallSteps.set(str(steps))
        Glyphs.defaults[PLUGIN_ID + ".waterfallStart"] = start
        Glyphs.defaults[PLUGIN_ID + ".waterfallSteps"] = steps
        self.gP.preview._waterfallLadder = ladder
        self.gP.preview.updateContentSize()

    def waterfallStartChanged_(self, sender):
        value = _finiteFloat(sender.get())
        if value is not None:  # mid-edit or not a number: keep the ladder
            self._applyWaterfallScale(start=value, echo=False)

    def waterfallStepsChanged_(self, sender):
        value = _finiteFloat(sender.get())
        if value is not None:
            self._applyWaterfallScale(steps=value, echo=False)

    def waterfallStartStepperChanged_(self, sender):
        self._applyWaterfallScale(start=sender.get())

    def waterfallStepsStepperChanged_(self, sender):
        self._applyWaterfallScale(steps=sender.get())

    def waterfallMultiColumnsChanged_(self, sender):
        on = bool(sender.get())
        self.gP.preview._waterfallMultiColumns = on
        Glyphs.defaults[PLUGIN_ID + ".waterfallMultiColumns"] = on
        self.gP.preview.updateContentSize()

    @objc.python_method
    def _refreshFocusPicker(self):
        """Repopulate the Waterfall picker; _focusKeys[row - 1] is the row's itemKey."""
        try:
            preview = self.gP.preview
            font = Glyphs.font
            showMasters = (
                preview.effectiveShowMasters()
            )  # Waterfall lists instances only
            items = displayItems(font, showMasters, preview.effectiveActiveOnly())
            names = []
            for it in items:
                try:
                    fam = familyResidue(it.familyName, font)
                except Exception:
                    fam = ""
                names.append((fam + " " + str(it.name or "Instance")).strip())
            keys = [itemKey(it, showMasters) for it in items]
            self._focusKeys = keys
            self.gS.focusPopup.setItems(["Current master"] + names)
            key = preview._focusKey
            self.gS.focusPopup.set(keys.index(key) + 1 if key in keys else 0)
        except Exception:
            print(traceback.format_exc())

    # Instances mode: show only active (exporting) instances
    def activeToggle_(self, sender):
        self._setActiveOnly(bool(sender.get()))

    @objc.python_method
    def _setActiveOnly(self, on):
        on = bool(on)
        Glyphs.defaults[PLUGIN_ID + ".activeOnly"] = on
        self.gP.preview._activeOnly = on
        self._refreshFocusPicker()
        self.gP.preview.updateContentSize()

    # view-options popover, rebuilt per open so it reflects the current state
    def showViewOptions_(self, sender):
        try:
            preview = self.gP.preview
            pop = Popover((210, 188), behavior="semitransient")
            for i, (attr, title, indent) in enumerate(VIEW_OPTIONS):
                # pop.coords, pop.guides, …: _syncDependentToggles reads them by name
                setattr(
                    pop,
                    attr[5:].lower(),
                    CheckBox(
                        (12 + 16 * indent, 8 + 22 * i, -12, 18),
                        title,
                        value=bool(getattr(preview, attr)),
                        callback=lambda sender, attr=attr: self._setViewOption(
                            attr, bool(sender.get())
                        ),
                        sizeStyle="small",
                    ),
                )
            self._viewPopover = pop  # retain so it isn't collected while shown
            self._syncDependentToggles()
            pop.open(parentView=sender, preferredEdge="bottom")
        except Exception:
            print(traceback.format_exc())

    # view-hosted checkboxes: a control in a custom-view NSMenuItem doesn't dismiss the
    # menu, so several options can be toggled per visit
    @objc.python_method
    def _addViewOptionItems(self, menu):
        preview = self.gP.preview
        header = menu.addItemWithTitle_action_keyEquivalent_("View Options", None, "")
        header.setEnabled_(False)
        self._voButtons = {}
        rowW, rowH = 210.0, 22.0
        for attr, title, indent in VIEW_OPTIONS:
            row = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, rowW, rowH))
            x = 14.0 + 16.0 * indent  # match the menu's text indent + child inset
            btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(x, 1.0, rowW - x - 4.0, 20.0)
            )
            btn.setButtonType_(3)  # NSButtonTypeSwitch (checkbox)
            btn.setTitle_(title)
            btn.setFont_(NSFont.menuFontOfSize_(13.0))
            btn.setState_(1 if bool(getattr(preview, attr)) else 0)
            btn.setEnabled_(self._viewOptionEnabled(attr))
            btn.setTarget_(self)
            btn.setAction_("viewOptionCheck:")
            btn.setIdentifier_(attr)
            row.addSubview_(btn)
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, None, ""
            )
            item.setView_(row)
            menu.addItem_(item)
            self._voButtons[attr] = btn

    # click-to-promote rows: a drag session can't run inside a menu tracking loop
    @objc.python_method
    def _addSortPriorityItems(self, menu, atIndex=None):
        """Insert a 'Sort priority' header + one row per axis at atIndex (default: end).
        Returns True if anything was added."""
        preview = self.gP.preview
        if bool(preview._waterfall):
            return False  # single-row mode: ordering is meaningless
        font = Glyphs.font
        tags = [a.axisTag for a in font.axes] if font else []
        saved = list(preview._axisOrder or [])
        order = [t for t in saved if t in tags] + [t for t in tags if t not in saved]
        if len(order) < 2:
            return False
        idx = menu.numberOfItems() if atIndex is None else atIndex
        header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Sort priority", None, ""
        )
        header.setEnabled_(False)
        header.setTag_(SORT_ROW_MENU_TAG)
        menu.insertItem_atIndex_(header, idx)
        idx += 1
        rowW = 210.0
        for i, tag in enumerate(order):
            row = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, rowW, 20.0))
            btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(14.0, 0.0, rowW - 18.0, 20.0)
            )
            btn.setButtonType_(0)  # NSButtonTypeMomentaryLight
            btn.setBordered_(False)
            btn.setTitle_(f"{i + 1}   {tag}")
            btn.setAlignment_(0)  # NSTextAlignmentLeft
            btn.setFont_(NSFont.menuFontOfSize_(13.0))
            btn.setTarget_(self)
            btn.setAction_("sortRowPicked:")
            btn.setIdentifier_(tag)
            row.addSubview_(btn)
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(tag, None, "")
            item.setView_(row)
            item.setTag_(SORT_ROW_MENU_TAG)
            menu.insertItem_atIndex_(item, idx)
            idx += 1
        return True

    def sortRowPicked_(self, sender):
        # the menu stays open, so re-render the rows in place to renumber them
        try:
            tag = str(sender.identifier())
            order = list(self.gP.preview._axisOrder or [])
            if tag in order:
                order.remove(tag)
            order.insert(0, tag)
            self._commitAxisOrder(order)
            item0 = sender.enclosingMenuItem()
            menu = item0.menu() if item0 is not None else None
            if menu is not None:
                first = None
                i = 0
                while i < menu.numberOfItems():
                    if menu.itemAtIndex_(i).tag() == SORT_ROW_MENU_TAG:
                        if first is None:
                            first = i
                        menu.removeItemAtIndex_(i)
                    else:
                        i += 1
                if first is not None:
                    self._addSortPriorityItems(menu, atIndex=first)
        except Exception:
            print(traceback.format_exc())

    def viewOptionCheck_(self, sender):
        try:
            self._setViewOption(str(sender.identifier()), bool(sender.state()))
            self._syncMenuViewOptions()  # menu stays open — refresh states/greying live
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _syncMenuViewOptions(self):
        preview = self.gP.preview
        for attr, btn in self._voButtons.items():
            try:
                btn.setState_(1 if bool(getattr(preview, attr)) else 0)
                btn.setEnabled_(self._viewOptionEnabled(attr))
            except Exception:
                pass

    # preview callback: the shift-click reference lock changed
    @objc.python_method
    def onLockChanged(self, key):
        Glyphs.defaults[PLUGIN_ID + ".lockedKey"] = key

    # preview callback after a row drag-reorder (manual order wins)
    @objc.python_method
    def onInstancesReordered(self, orderedKeys):
        Glyphs.defaults[PLUGIN_ID + ".customOrder"] = list(orderedKeys)
        self.gP.preview.updateContentSize()

    @objc.python_method
    def _commitAxisOrder(self, tags):
        tags = list(tags)
        Glyphs.defaults[PLUGIN_ID + ".axisOrder"] = tags
        self.gP.preview._axisOrder = tags
        # an axis reorder means "sort by axis": drop the manual order that overrides it
        if self.gP.preview._customOrder:
            self.gP.preview._customOrder = []
            Glyphs.defaults[PLUGIN_ID + ".customOrder"] = []
        self.gP.preview.updateContentSize()

    @objc.python_method
    def refreshAxisOrder(self):
        """Sync the sort priority to the font's axes, keeping the saved order."""
        font = Glyphs.font
        tags = [a.axisTag for a in font.axes] if font else []
        saved = list(Glyphs.defaults[PLUGIN_ID + ".axisOrder"] or [])
        order = [t for t in saved if t in tags]
        for t in tags:
            if t not in order:
                order.append(t)
        self.gP.preview._axisOrder = order
        self._refreshFocusPicker()
        self._layoutSidebar()

    # UPDATEINTERFACE: mirror the Edit tab while the sample field is empty
    def syncFromTab_(self, sender):
        if self._hidden:
            return  # warm-hidden: _wake refreshes it
        try:
            if self.gS.edit.get():
                return
        except Exception:
            return
        names = self.tabGlyphNames()
        if names == self._lastTabList:
            return
        self._lastTabList = names
        self.gP.preview.setGlyphs(names)
        self.gP.preview.updateContentSize()

    @objc.python_method
    def tabGlyphNames(self):
        font = self._cur_font()
        tab = font.currentTab if font else None
        if tab is None:
            return []
        names = []
        # .values() lists the layers once; iterating the proxy re-lists them per index
        # on Glyphs 4 (O(N^2))
        for layer in tab.layers.values():
            try:
                if layer.className() == "GSControlLayer":
                    continue
            except Exception:
                pass
            parent = getattr(layer, "parent", None)
            name = parent.name if parent is not None else None
            if name:
                names.append(name)
        return names

    @objc.python_method
    def _removeSyncCallback(self):
        try:
            Glyphs.removeCallback(self.syncFromTab_, UPDATEINTERFACE)
        except Exception:
            pass

    def liveEditChanged_(self, notification):
        # UPDATEINTERFACE fires per drag step: debounce the costly signature check
        try:
            if self._hidden:
                return
            NSObject.cancelPreviousPerformRequestsWithTarget_selector_object_(
                self, b"liveCheck:", None
            )
            self.performSelector_withObject_afterDelay_(
                b"liveCheck:", None, LIVE_REDRAW_DELAY
            )
        except Exception:
            print(traceback.format_exc())

    def liveCheck_(self, _):
        # the preview's path cache keys on _liveSig. Never interpolates here, so it
        # can't recurse into UPDATEINTERFACE.
        try:
            if self._hidden:
                return
            sig = self.gP.preview.outlineSignature()
            if sig == self.gP.preview._liveSig:
                return
            self.gP.preview._liveSig = sig
            self.gP.preview.redraw()
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _removeLiveCallback(self):
        try:
            Glyphs.removeCallback(self.liveEditChanged_, UPDATEINTERFACE)
        except Exception:
            pass
        try:
            NSObject.cancelPreviousPerformRequestsWithTarget_selector_object_(
                self, b"liveCheck:", None
            )
        except Exception:
            pass

    def changeDocument_(self, sender):
        if self._closed:
            return  # window gone but a stray callback fired
        if self._hidden:
            # warm-hidden: keep the warm cache; _wake() rebuilds for the current font
            self._dirtyWhileHidden = True
            return
        try:
            font = Glyphs.font
            self._fontCached = (
                font  # reseed the _cur_font TTL cache — doc switches are never stale
            )
            self._fontStamp = time.time()
            # DOCUMENTACTIVATED also fires on app re-activation (same font): wipe caches
            # only on a real font change, else every return re-interpolates (freeze)
            try:
                # a closed font's address can be reused by the next font: pair the
                # pointer with stable attrs so a different font never matches
                pid = objc.pyobjc_id(font) if font is not None else None
                fid = (
                    (pid, str(font.familyName), int(font.upm or 0), len(font.masters))
                    if font is not None
                    else None
                )
            except Exception:
                fid = id(font) if font is not None else None
            fontChanged = fid != getattr(self, "_lastDocFid", None)
            self._lastDocFid = fid
            if fontChanged:
                self.gP.preview.clearCache()
                self.gP.preview._liveSig = None
                self.refreshAxisOrder()
                self._lastTabList = None
                self.syncFromTab_(None)
                self.gP.preview.updateContentSize()
            # the new document may be in Font view (hide) or on an Edit tab (restore)
            self._syncPanelToTab()
        except Exception:
            pass

    def windowClosed_(self, sender):
        self._closed = (
            True  # set FIRST so a throw below can't leave stale callbacks "live"
        )
        self._floatToolbarBuilt = False  # a reopen builds a fresh window
        # narrow hosts don't own the floating frame
        try:
            if not self._narrowHosted():
                nsw = self.w.getNSWindow()
                if nsw is not None:
                    f = nsw.frame()
                    Glyphs.defaults[PLUGIN_ID + ".winFrame"] = json.dumps(
                        [
                            float(f.origin.x),
                            float(f.origin.y),
                            float(f.size.width),
                            float(f.size.height),
                        ]
                    )
        except Exception:
            pass
        # independent: one throwing must not skip the rest. The remote server + snapshot
        # timer deliberately outlive the window.
        for teardown in (self._removeSyncCallback, self._removeLiveCallback):
            try:
                teardown()
            except Exception:
                pass
        # addCallback retains the bound method: remove it here
        try:
            Glyphs.removeCallback(self.changeDocument_, DOCUMENTACTIVATED)
        except Exception:
            pass
        # a still-visible G4 sidebar tab takes the panel back (else it shows a stale
        # placeholder); deferred so the close teardown settles first
        try:
            if self._visibleSidebarContainer() is not None:
                self.performSelector_withObject_afterDelay_(
                    b"readoptSidebar:", None, 0.0
                )
        except Exception:
            pass

    def readoptSidebar_(self, sender):
        try:
            cont = self._visibleSidebarContainer()
            if cont is not None:
                self.adoptSidebarHost(cont)
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _visibleSidebarContainer(self):
        """The G4 sidebar tab's container while it's on screen, else None."""
        cont = self._sidebarTabContainer
        if (
            cont is not None
            and cont.window() is not None
            and not cont.isHiddenOrHasHiddenAncestor()
        ):
            return cont
        return None

    @objc.python_method
    def makeList(self, string):
        """Sample string → glyph tokens; honours /glyphName, skips emoji modifiers."""
        if not string:
            return []
        try:
            newList = list(
                string.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
            )
            filtered = []
            skip = 0
            # hoisted: Glyphs.font is a ~0.6 ms XPC and the /name probe loops per char
            font = self._cur_font()
            for i, c in enumerate(newList):
                if i < skip:
                    continue
                if emoji_modifier.match(c):
                    continue
                elif c == "/":
                    if i + 1 > len(newList) - 1:
                        filtered.append(c)
                        continue
                    longest = "".join(newList[i + 1 :])
                    while longest:
                        if font and font.glyphs[longest]:
                            filtered.append(longest)
                            skip = i + len(longest) + 1
                            break
                        longest = longest[:-1]
                else:
                    filtered.append(c)
            return filtered
        except Exception:
            print("AZ Super Preview error (makeList)", traceback.format_exc())
            return []

    def __file__(self):
        """Please leave this method unchanged"""
        return __file__
