"""A speculative text reply: buffer model output until the surface chooses its speaker."""
import contextlib
import queue
import threading


class ReplyCancel:
    """Cancel this request without discarding its turn; also observe turn cancellation.

    Accounting waits for the verdict even if the provider finishes first. An unused
    request is cancelled, with any usage already reported by the provider retained.
    """
    def __init__(self, turn_cancel):
        self.turn_cancel = turn_cancel
        self.verdict = threading.Event()
        self.accepted = False

    def is_set(self):
        return self.turn_cancel.is_set() or (self.verdict.is_set() and not self.accepted)

    def choose(self, accepted):
        self.accepted = accepted
        self.verdict.set()

    def finish_status(self, status):
        while not self.verdict.wait(0.05):
            if self.turn_cancel.is_set():
                break
        return "cancelled" if self.is_set() else status


class TextReply:
    def __init__(self, prepare, parts, config, turn_cancel):
        self.cancel = ReplyCancel(turn_cancel)
        self.items = queue.Queue()
        # Capture the bridge's turn/memory context on the caller, before starting a thread.
        stream = prepare(parts, config, self.cancel)

        def run():
            try:
                with contextlib.closing(stream()) as chunks:
                    for chunk in chunks:
                        if self.cancel.is_set():
                            break
                        self.items.put((chunk, None))
            except Exception as error:
                self.items.put((None, error))
            finally:
                self.items.put((None, None))

        self.thread = threading.Thread(target=run, daemon=True, name="text-reply-deep")
        self.thread.start()

    def choose(self, accepted):
        self.cancel.choose(accepted)

    def chunks(self):
        while not self.cancel.is_set():
            try:
                chunk, error = self.items.get(timeout=0.05)
            except queue.Empty:
                continue
            if error is not None:
                raise error
            if chunk is None:
                return
            yield chunk
