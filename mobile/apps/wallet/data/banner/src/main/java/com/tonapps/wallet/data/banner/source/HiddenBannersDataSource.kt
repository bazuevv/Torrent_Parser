package com.tonapps.wallet.data.banner.source

import android.content.Context
import androidx.core.content.edit

internal class HiddenBannersDataSource(context: Context) {

    private val prefs = context.getSharedPreferences("banners_hidden", Context.MODE_PRIVATE)

    fun getHidden(walletId: String): Set<String> {
        return prefs.getStringSet(key(walletId), null)?.toSet() ?: emptySet()
    }

    fun hide(walletId: String, bannerId: String) {
        val hidden = getHidden(walletId).toMutableSet()
        if (hidden.add(bannerId)) {
            prefs.edit { putStringSet(key(walletId), hidden) }
        }
    }

    fun clear(walletId: String) {
        prefs.edit { remove(key(walletId)) }
    }

    private fun key(walletId: String) = "hidden_$walletId"
}
