package com.tonapps.tonkeeper.ui.screen.name.edit

import android.app.Application
import android.net.Uri
import androidx.lifecycle.viewModelScope
import com.tonapps.tonkeeper.core.FirebaseHelper
import com.tonapps.tonkeeper.ui.base.BaseWalletVM
import com.tonapps.tonkeeper.worker.WidgetUpdaterWorker
import com.tonapps.wallet.data.account.AccountRepository
import com.tonapps.wallet.data.settings.SettingsRepository
import com.tonapps.blockchain.model.legacy.WalletEntity
import kotlinx.coroutines.launch

class EditNameViewModel(
    app: Application,
    private val wallet: WalletEntity,
    private val accountRepository: AccountRepository,
    private val settingsRepository: SettingsRepository
): BaseWalletVM(app) {

    val photoPath: String?
        get() = settingsRepository.getWalletAvatarPhoto(wallet.id)

    /**
     * Копирует выбранное изображение к себе и делает его аватаром.
     * [onDone] получает путь к файлу либо null, если картинку не удалось прочитать.
     */
    fun setPhoto(uri: Uri, onDone: (path: String?) -> Unit) {
        viewModelScope.launch {
            val path = settingsRepository.setWalletAvatarPhoto(wallet.id, uri)
            onDone(path)
            WidgetUpdaterWorker.update(context)
        }
    }

    fun removePhoto() {
        settingsRepository.removeWalletAvatarPhoto(wallet.id)
        WidgetUpdaterWorker.update(context)
    }

    fun save(name: String, emoji: CharSequence, color: Int) {
        FirebaseHelper.setTitleEmoji(emoji.toString())
        accountRepository.editLabel(
            walletId = wallet.id,
            name = name,
            emoji = emoji,
            color = color
        )

        WidgetUpdaterWorker.update(context)
    }
}