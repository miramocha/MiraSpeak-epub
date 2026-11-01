"""Open an EPUB and read it aloud from any sentence."""

from __future__ import annotations

from miraspeak.modules.api import (
    LIVE,
    HostBusyError,
    LineEvent,
    LineState,
    Module,
    ModuleContext,
    ModuleError,
    ReadAlong,
    ui,
)

from .epub import EpubBook, EpubError, load_epub

NO_BOOK = ui.Empty(
    "No book open",
    "Pick an .epub file to start reading.",
    action="book",
    action_label="Open File…",
    art="file",
)
EMPTY_CHAPTER = ui.Empty("This chapter has no text")
OPENING = ui.Empty("Opening book…")


class EpubModule(Module):
    def page(self) -> ui.Page:
        return ui.Page(
            intro="Open an EPUB and read it aloud, following the sentence.",
            sections=(
                ui.Section(
                    "",
                    (
                        ui.Toggle("autoplay", "Autoplay", default=True),
                        ui.Button("stop", "Stop", enabled=False),
                        ui.File("book", extensions=("epub",)),
                        ui.Button("close", "Close", enabled=False, header=True),
                        ui.Choice("chapter", enabled=False),
                    ),
                    bar=True,
                ),
                ui.Section(
                    "",
                    (
                        ui.Status("status"),
                        ui.Rows("lines", click="read", when_empty=NO_BOOK, speaks=True),
                    ),
                ),
            ),
        )

    def on_start(self, ctx: ModuleContext) -> None:
        self.ctx = ctx
        self.along = ReadAlong()
        self.book: EpubBook | None = None
        self._hits: list[list[bool]] = []
        self._warning = False
        self._viewed = 0
        self._cursor: tuple[int, int] | None = None
        self._filling = False
        self._fill_again = False
        self.along.set_autoplay(bool(ctx.values.get("autoplay", True)))
        # Opening the saved book asks the app for the live voice. At launch that
        # call is queued before the Qt event loop runs, so do it after start and
        # retry instead of failing the module.
        if isinstance(ctx.values.get("book"), str) and ctx.values.get("book"):
            ctx.ui.set_empty("lines", OPENING)
            ctx.call_later(0, self._open_saved)

    def _open_saved(self) -> None:
        path = self.ctx.values.get("book")
        if not isinstance(path, str) or not path:
            return
        try:
            self._open(path)
        except HostBusyError:
            self.ctx.call_later(0.2, self._open_saved)

    def on_value(self, key: str, value: object) -> None:
        if key == "book" and isinstance(value, str):
            self._open(value)
        elif key == "chapter":
            self._stop_and_show(value)
        elif key == "autoplay":
            self.along.set_autoplay(bool(value))

    def on_voices_changed(self) -> None:
        try:
            self._refresh_cache()
        except HostBusyError:
            self.ctx.call_later(0.2, self.on_voices_changed)

    def on_action(self, action: str, row: int | None = None) -> None:
        if action == "stop":
            self._pause()
        elif action == "close":
            self._close()
        elif action == "read" and row is not None:
            self._read_from(row)

    def on_line(self, event: LineEvent) -> None:
        if not event.mine:
            return
        update = self.along.on_line(event.line_id, event.state)
        if update is None:
            return
        if event.state is LineState.PLAYING and update.highlight is not None:
            self._touch(update.highlight)
        if update.show and update.highlight is not None:
            self._show_tag(update.highlight)
        if update.fill:
            self._fill(self.along.generation)
        elif update.paused:
            self._sync_stop()

    def _open(self, path: str) -> None:
        self._pause()
        try:
            book = load_epub(path)
        except EpubError as exc:
            self._unload()
            self._warning = True
            self.ctx.ui.set_status("status", str(exc), tone=ui.TONE_WARNING)
            return
        self.book = book
        self._sync_loaded()
        self._scan()
        options = tuple((str(index), chapter.label) for index, chapter in enumerate(book.chapters))
        self.ctx.ui.set_options("chapter", options)
        current = self.ctx.values.get("chapter")
        chosen = (
            str(current) if str(current) in {option_id for option_id, _label in options} else "0"
        )
        self._show_section(int(chosen), highlight=None)

    def _close(self) -> None:
        self._pause()
        self._unload()
        self._warning = False
        self.ctx.ui.set("book", "")
        self.ctx.ui.set_status("status", "")

    def _unload(self) -> None:
        self.book = None
        self._hits = []
        self._viewed = 0
        self.ctx.ui.set_options("chapter", ())
        self.ctx.ui.set_rows("lines", ())
        self.ctx.ui.set_empty("lines", NO_BOOK)
        self._sync_loaded()

    def _stop_and_show(self, value: object) -> None:
        self._pause()
        if not str(value).isdigit():
            return
        self._show_section(int(value), highlight=None)

    def _pause(self) -> None:
        self.along.halt()
        self._cursor = None
        self.ctx.stop_mine()
        self.ctx.ui.highlight_row("lines", None)
        self._sync_stop()

    def _read_from(self, row: int) -> None:
        autoplay = bool(self.ctx.values.get("autoplay", True))
        generation = self.along.start(autoplay=autoplay)
        self._cursor = (self._viewed, row)
        self.ctx.stop_mine()
        self._fill(generation)

    def _fill(self, generation: int) -> None:
        if self._filling:
            self._fill_again = True
            return
        self._filling = True
        try:
            self._queue(generation)
        finally:
            self._filling = False
            if self._fill_again:
                self._fill_again = False
                self._fill(generation)
            else:
                self._sync_stop()

    def _queue(self, generation: int) -> None:
        while self.along.wants_another(generation):
            nxt = self._take()
            if nxt is None:
                return
            section, index, text = nxt
            try:
                result = self.ctx.speak(text, voice=LIVE, split=False)
            except ModuleError as exc:
                self.along.halt()
                self._warning = True
                self.ctx.ui.set_status("status", str(exc), tone=ui.TONE_WARNING)
                return
            line_id = result.line_ids[0] if result.line_ids else None
            if line_id is None or not self.along.note(generation, line_id, (section, index)):
                if result.line_ids:
                    self.ctx.stop(result)
                return
            self._sync_stop()

    def _take(self) -> tuple[int, int, str] | None:
        if self.book is None or self._cursor is None:
            return None
        section, sentence = self._cursor
        chapters = self.book.chapters
        autoplay = bool(self.ctx.values.get("autoplay", True))
        while section < len(chapters):
            lines = chapters[section].sentences
            if sentence < len(lines):
                text = lines[sentence]
                self._cursor = (section, sentence + 1)
                return section, sentence, text
            if not autoplay:
                return None
            section += 1
            sentence = 0
            self._cursor = (section, sentence)
        return None

    def _show_tag(self, tag: object) -> None:
        parsed = _tag(tag)
        if parsed is None:
            return
        section, index = parsed
        if section != self._viewed:
            self.ctx.ui.set("chapter", str(section))
            self._show_section(section, highlight=index)
            return
        self.ctx.ui.highlight_row("lines", index)

    def _show_section(self, section: int, *, highlight: int | None) -> None:
        self._viewed = section
        if self.book is None or not 0 <= section < len(self.book.chapters):
            self.ctx.ui.set_rows("lines", ())
            return
        lines = self.book.chapters[section].sentences
        self._warning = False
        if lines:
            self._set_counts()
        else:
            self.ctx.ui.set_empty("lines", EMPTY_CHAPTER)
            self.ctx.ui.set_status("status", "")
        self.ctx.ui.set_rows("lines", lines)
        self._paint_marks()
        self.ctx.ui.highlight_row("lines", highlight)

    def _scan(self) -> None:
        if self.book is None:
            self._hits = []
            return
        flat = tuple(sentence for chapter in self.book.chapters for sentence in chapter.sentences)
        flags = self.ctx.cached(flat) if flat else ()
        self._hits = []
        cursor = 0
        for chapter in self.book.chapters:
            count = len(chapter.sentences)
            self._hits.append(list(flags[cursor : cursor + count]))
            cursor += count

    def _touch(self, tag: object) -> None:
        parsed = _tag(tag)
        if parsed is None or self.book is None:
            return
        section, index = parsed
        if not 0 <= section < len(self.book.chapters):
            return
        sentences = self.book.chapters[section].sentences
        if not 0 <= index < len(sentences) or section >= len(self._hits):
            return
        hit = bool(self.ctx.cached((sentences[index],))[0])
        if index >= len(self._hits[section]):
            return
        self._hits[section][index] = hit
        if not self._warning:
            self._set_counts()
        if section == self._viewed:
            self._paint_marks()

    def _paint_marks(self) -> None:
        if not 0 <= self._viewed < len(self._hits):
            return
        indexes = tuple(i for i, hit in enumerate(self._hits[self._viewed]) if hit)
        self.ctx.ui.mark_rows("lines", indexes)

    def _set_counts(self) -> None:
        if self._warning or not self._hits:
            return
        chapter = self._hits[self._viewed] if 0 <= self._viewed < len(self._hits) else []
        chapter_hit = sum(chapter)
        book_hit = sum(sum(flags) for flags in self._hits)
        book_total = sum(len(flags) for flags in self._hits)
        text = (
            f"{chapter_hit} of {len(chapter)} in this chapter"
            f" · {book_hit} of {book_total} in the book"
        )
        self.ctx.ui.set_status("status", text)

    def _sync_stop(self) -> None:
        self.ctx.ui.set_enabled("stop", self.along.busy)

    def _sync_loaded(self) -> None:
        loaded = self.book is not None
        self.ctx.ui.set_enabled("close", loaded)
        self.ctx.ui.set_enabled("chapter", loaded)

    def _refresh_cache(self) -> None:
        self._scan()
        if self.book is None:
            return
        if not self._warning:
            self._set_counts()
        self._paint_marks()


def _tag(tag: object) -> tuple[int, int] | None:
    if not isinstance(tag, tuple) or len(tag) != 2:
        return None
    section, index = tag
    if isinstance(section, bool) or isinstance(index, bool):
        return None
    if isinstance(section, int) and isinstance(index, int):
        return section, index
    return None
