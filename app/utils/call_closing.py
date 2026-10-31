"""Bounded closing timers; no language-model calls while waiting for speech."""
import asyncio
import contextlib
import re

# Ask, silence warning, farewell. Native-script copy is also the output contract
# for detecting the single final offer; never infer readiness from arbitrary '?'s.
CLOSING = {
    'hinglish': ('कोई खास request है?', 'आपकी details मैंने ले ली हैं। यह call चार सेकंड में बंद हो जाएगी।', 'आपके समय के लिए thank you। नमस्ते।'),
    'en': ("Do you have any specific requests?", "I've got the details you've shared. This call will end in four seconds.", "Thanks for sharing your request. Goodbye."),
    'hi': ("क्या आपकी कोई खास गुज़ारिश है?", "आपने जो जानकारी दी है, मैंने ले ली है। यह कॉल चार सेकंड में समाप्त हो जाएगी।", "अपनी बात साझा करने के लिए धन्यवाद। नमस्ते।"),
    'bn': ("আপনার কি কোনো বিশেষ অনুরোধ আছে?", "আপনার দেওয়া তথ্য পেয়েছি। চার সেকেন্ডের মধ্যে এই কল শেষ হবে।", "আপনার অনুরোধ জানানোর জন্য ধন্যবাদ। ভালো থাকবেন।"),
    'ta': ("உங்களுக்கு ஏதேனும் சிறப்பு வேண்டுகோள் உள்ளதா?", "நீங்கள் பகிர்ந்த விவரங்களைப் பெற்றுள்ளேன். இந்த அழைப்பு நான்கு நொடிகளில் முடியும்.", "உங்கள் கோரிக்கையைப் பகிர்ந்ததற்கு நன்றி. வணக்கம்."),
    'te': ("మీకు ఏవైనా ప్రత్యేక అభ్యర్థనలు ఉన్నాయా?", "మీరు చెప్పిన వివరాలు తీసుకున్నాను. ఈ కాల్ నాలుగు సెకన్లలో ముగుస్తుంది.", "మీ అభ్యర్థనను పంచుకున్నందుకు ధన్యవాదాలు. నమస్కారం."),
    'gu': ("તમારી કોઈ ખાસ વિનંતી છે?", "તમે આપેલી વિગતો મળી ગઈ છે. આ કૉલ ચાર સેકન્ડમાં પૂરો થશે.", "તમારી વિનંતી જણાવવા બદલ આભાર. આવજો."),
    'kn': ("ನಿಮಗೆ ಯಾವುದಾದರೂ ವಿಶೇಷ ವಿನಂತಿಗಳಿವೆಯೇ?", "ನೀವು ನೀಡಿದ ವಿವರಗಳನ್ನು ಪಡೆದಿದ್ದೇನೆ. ಈ ಕರೆ ನಾಲ್ಕು ಸೆಕೆಂಡುಗಳಲ್ಲಿ ಮುಗಿಯುತ್ತದೆ.", "ನಿಮ್ಮ ವಿನಂತಿಯನ್ನು ತಿಳಿಸಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು. ನಮಸ್ಕಾರ."),
    'ml': ("നിങ്ങൾക്ക് പ്രത്യേക അഭ്യർത്ഥനകളുണ്ടോ?", "നിങ്ങൾ നൽകിയ വിവരങ്ങൾ ലഭിച്ചു. ഈ കോൾ നാല് സെക്കൻഡിനുള്ളിൽ അവസാനിക്കും.", "നിങ്ങളുടെ അഭ്യർത്ഥന അറിയിച്ചതിന് നന്ദി. വിട."),
    'mr': ("तुमची काही खास विनंती आहे का?", "तुम्ही दिलेली माहिती घेतली आहे. हा कॉल चार सेकंदांत संपेल.", "तुमची विनंती सांगितल्याबद्दल धन्यवाद. नमस्कार."),
    'pa': ("ਕੀ ਤੁਹਾਡੀ ਕੋਈ ਖਾਸ ਬੇਨਤੀ ਹੈ?", "ਤੁਹਾਡੇ ਦਿੱਤੇ ਵੇਰਵੇ ਮਿਲ ਗਏ ਹਨ। ਇਹ ਕਾਲ ਚਾਰ ਸਕਿੰਟਾਂ ਵਿੱਚ ਖਤਮ ਹੋ ਜਾਵੇਗੀ।", "ਆਪਣੀ ਬੇਨਤੀ ਦੱਸਣ ਲਈ ਧੰਨਵਾਦ। ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ।"),
    'od': ("ଆପଣଙ୍କର କୌଣସି ବିଶେଷ ଅନୁରୋଧ ଅଛି କି?", "ଆପଣ ଦେଇଥିବା ବିବରଣୀ ମିଳିଛି। ଏହି କଲ୍ ଚାରି ସେକେଣ୍ଡରେ ଶେଷ ହେବ।", "ଆପଣଙ୍କ ଅନୁରୋଧ ଜଣାଇଥିବାରୁ ଧନ୍ୟବାଦ। ନମସ୍କାର।"),
    'es': ("¿Tienes alguna petición especial?", "He recibido los detalles que compartiste. Esta llamada terminará en cuatro segundos.", "Gracias por compartir tu solicitud. Hasta luego."),
    'de': ("Haben Sie besondere Wünsche?", "Ich habe Ihre Angaben aufgenommen. Dieser Anruf endet in vier Sekunden.", "Vielen Dank für Ihre Anfrage. Auf Wiederhören."),
    'it': ("Hai qualche richiesta particolare?", "Ho raccolto i dettagli che hai condiviso. Questa chiamata terminerà tra quattro secondi.", "Grazie per aver condiviso la tua richiesta. Arrivederci."),
    'ar': ("عندك أي طلب خاص؟", "سجّلت التفاصيل اللي ذكرتها. المكالمة بتنتهي بعد أربع ثواني.", "شكرًا على طلبك. مع السلامة."),
}


def specific_offer(text):
    value = text.strip().casefold()
    return any(value.endswith(row[0].casefold()) for row in CLOSING.values()) or bool(re.search(
        r'(?:any (?:other )?(?:specific|special) requests|any special requirements)\s*\?\s*$', value))


def final_refusal(text):
    value = re.sub(r'[.!?,।\s]+$', '', text.strip().casefold())
    return bool(re.fullmatch(
        r"(?:no|nope|nah)(?:[, ]+(?:thanks|thank you|that['’]s all|that is all|that['’]s it|nothing else|no special requests))?"
        # Seen live: a bare "Nothing." after "anything else?" got the help menu instead of a goodbye.
        r"|nothing(?: else| more)?(?:[, ]+(?:thanks|thank you))?|that['’]s (?:all|it)(?:[, ]+(?:thanks|thank you))?"
        r"|kuch nahi|bas|कुछ नहीं"
        r"|no (?:specific|special|other) requests|नहीं(?:[, ]+(?:धन्यवाद|बस|और कुछ नहीं))?"
        r"|बस इतना ही|कोई खास (?:अनुरोध|गुज़ारिश) नहीं|না|இல்லை|లేదు|ના|ಇಲ್ಲ|ഇല്ല|नाही|ਨਹੀਂ|ନା|nein|no grazie", value))


def closing_coaching(language, already_offered):
    question = CLOSING.get(language, CLOSING['en'])[0]
    if already_offered:
        return ("The specific-request question has already been asked. Do not ask it or a generic final-help question again. "
                "If they say yes without a request, ask them what they would like. Otherwise address the new request or correction briefly. "
                "Do not promise a booking or contact. The server handles silent-call closure; never announce a countdown yourself.")
    return ("CALL COMPLETION: Collect ONLY essentials named in business context; never invent extra required fields. "
            "A preferred day and period such as Saturday evening satisfies timing; do not demand an exact hour. "
            "A supplied service area satisfies area; do not demand a street address unless explicitly required. "
            "Access instructions, doorbell preferences and similar requests CAN be recorded without promising to perform them. "
            "When all essential details for the caller's chosen need are collected (or explicitly declined/unknown), "
            "answer any outstanding question, then ask this final question ONCE, exactly in the active language: "
            + question + " Do not ask it while essential details remain unresolved, on an unrelated topic, or in the greeting. "
            "After that, let the caller speak. The server owns the silence timer. Do not invent a timer or a booking.")


class CallClosing:
    """Mixin owned by one DemoSession; every timer is cancelled on cleanup/input."""
    def init_closing(self):
        self.closing_task = None
        self.closing_phase = 'open'
        self.completion_pending = False
        self.playback_event = asyncio.Event()
        self.playback_generation = -1
        self.close_reason = 'browser_disconnected'

    async def cancel_closing(self):
        task, self.closing_task = self.closing_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.closing_phase not in {'open', 'finished'}:
            await self.send('closing_cancelled')
            self.closing_phase = 'open'

    def reset_playback(self, generation):
        self.playback_event = asyncio.Event()
        self.playback_generation = generation

    async def wait_playback(self, generation):
        if not self.tts: return
        event = self.playback_event
        if self.playback_generation != generation: return
        try:
            await asyncio.wait_for(event.wait(), self.settings.studio_playback_timeout_s)
        except asyncio.TimeoutError:
            await self.trace_event('closing', stage='playback_timeout', generation=generation)

    def arm_closing(self, farewell=False):
        if self.finished or self.closed: return
        if self.closing_task and not self.closing_task.done(): return
        self.closing_phase = 'awaiting_playback'
        self.closing_task = asyncio.create_task(self._closing_flow(farewell))

    async def caller_activity(self):
        """Cancel countdown on microphone onset before speech recognition finishes."""
        if not self.completion_pending or self.finished or self.closed: return
        await self.cancel_closing()
        if self.playing: await self.interrupt()
        async def resume_if_no_transcript():
            await asyncio.sleep(self.settings.studio_playback_timeout_s)
            self.closing_task = None
            self.arm_closing()
        self.closing_phase = 'waiting_for_speech'
        self.closing_task = asyncio.create_task(resume_if_no_transcript())

    def silence_warning(self):
        """Spoken after the final offer meets silence, before the countdown. A session may override it."""
        return CLOSING.get(self.language, CLOSING['en'])[1]

    async def _closing_flow(self, farewell):
        try:
            generation = self.generation
            await self.wait_playback(generation)
            if farewell:
                self.close_reason = 'caller_finished'
            else:
                self.closing_phase = 'waiting'
                await self.trace_event('closing', stage='waiting_for_specific_request')
                await asyncio.sleep(self.settings.studio_final_reply_wait_s)
                if self.finished or self.closed: return
                self.closing_phase = 'warning'
                await self.trace_event('closing', stage='silence_warning')
                async with self.lock:
                    await self.interrupt()
                    self.turn = asyncio.create_task(self.respond(script=self.silence_warning()))
                await self.turn
                await self.wait_playback(self.generation)
                self.closing_phase = 'countdown'
                await self.send('closing_countdown', seconds=4)
                await asyncio.sleep(4)
                self.close_reason = 'silence_after_specific_request'
            self.closing_phase = 'finished'
            await self.trace_event('closing', stage='finished', reason=self.close_reason)
            await self.finish()
            await self.ws.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never leave a failed timer burning provider time in the background.
            if not self.trace_failed:
                with contextlib.suppress(Exception):
                    await self.trace_event('error', stage='closing', code=type(exc).__name__)
                    await self.send('error', message=('Call खत्म हो गई है। Saved conversation history में है।' if self.language == 'hinglish' else 'The call has ended. Saved turns remain in history.'))
            with contextlib.suppress(Exception): await self.ws.close()
