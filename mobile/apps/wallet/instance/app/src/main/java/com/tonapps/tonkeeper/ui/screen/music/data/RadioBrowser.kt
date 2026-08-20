package com.tonapps.tonkeeper.ui.screen.music.data

object RadioBrowser {

    /**
     * Официальные зеркала API (radio-browser.info). Каталог общий у всех,
     * поэтому пробуем по очереди, пока какое-нибудь не ответит.
     */
    val MIRRORS = listOf(
        "de1.api.radio-browser.info",
        "nl1.api.radio-browser.info",
        "at1.api.radio-browser.info",
        "fi1.api.radio-browser.info",
        "de2.api.radio-browser.info",
    )

    // Топ станций России по голосам сообщества, без отвалившихся
    const val STATIONS_PATH =
        "/json/stations/bycountrycodeexact/RU?order=votes&reverse=true&limit=500&hidebroken=true"
}
