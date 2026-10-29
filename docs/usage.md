# Using the gateway

Select a connected machine, then a tmux session. Existing sessions run under the node agent's user account. The session picker can create sessions; a normal shell is the general-purpose option. Optional Claude integration requires that CLI already be installed and retains its normal permission prompts.

## Selecting, copying and links

- Drag on the terminal to select text; double-click selects a word.
- Command+C or Ctrl+C copies a selection. Without a selection, Ctrl+C is sent as an interrupt.
- Alt/Option-click or drag deliberately forwards mouse input to the terminal application.
- Click an HTTP(S) URL or labeled terminal hyperlink to open it. Dragging over a link selects it.
- History opens a stable, searchable snapshot suitable for native text selection. A late history response does not replace a snapshot while you are selecting.
- Clipboard APIs depend on browser permissions and secure context. A fallback is provided, but physical-device behavior varies.

## Scrolling and touch

Wheel/trackpad and touch gestures use local scrollback when possible. Mouse-aware applications receive actual mouse reports; other remote history uses tmux copy mode. Scrolling does not send Up/Down keys that would recall prompts.

The compact key controls provide Escape, modifier keys and navigation keys. Ctrl and Alt apply to the next key. A web app cannot insert custom keys into the operating system's software keyboard. Physical mobile keyboards and selection handles remain an alpha testing area.

## Tabs and control

Closing a tab keeps the underlying session running. Reordering tabs affects your browser's saved order. Only the selected tab attaches to its terminal stream.

A new viewer does not automatically displace the current controller. Typing or
pasting into the terminal takes control; on a phone, tapping plain terminal text
or the keyboard button also takes control. The first keystroke follows the claim
on the same connection. Scrolling, selecting text, and opening links do not claim
control. The existing control button still lets you take or release it explicitly.
Viewing without input control does not reduce a user's privileges elsewhere in
the gateway.

## Updates

Reload browser tabs after a gateway update. Preserve the gateway CA/configuration and node credentials. Do not run two agents with the same registered name. Test upgrades against a disposable tmux socket before changing a running work session.
