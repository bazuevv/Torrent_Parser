package com.tonapps.tonkeeper.ui.screen.tv.settings

import android.os.Bundle
import android.view.View
import android.widget.Button
import com.tonapps.tonkeeper.extensions.hideKeyboard
import com.tonapps.tonkeeper.ui.base.BaseWalletScreen
import com.tonapps.tonkeeper.ui.base.BaseWalletVM
import com.tonapps.tonkeeper.ui.base.ScreenContext
import com.tonapps.tonkeeper.ui.screen.tv.data.TvPlaylistRepository
import com.tonapps.tonkeeperx.R
import org.koin.android.ext.android.inject
import uikit.base.BaseFragment
import uikit.extensions.pinToBottomInsets
import uikit.widget.InputView
import uikit.widget.ModalHeader

/**
 * Адрес M3U-плейлиста. Экран пишет прямо в репозиторий: список каналов
 * подписан на его `playlistUrlFlow` и перезагрузится сам.
 */
class TvSettingsScreen : BaseWalletScreen<ScreenContext.None>(
    R.layout.fragment_tv_settings,
    ScreenContext.None
), BaseFragment.Modal {

    override val fragmentName: String = "TvSettingsScreen"

    override val viewModel: BaseWalletVM? = null

    private val playlistRepository: TvPlaylistRepository by inject()

    private lateinit var headerView: ModalHeader
    private lateinit var urlView: InputView
    private lateinit var resetButton: Button
    private lateinit var button: Button

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        headerView = view.findViewById(R.id.header)
        headerView.onCloseClick = { finish() }

        urlView = view.findViewById(R.id.url)
        // Подборка по умолчанию в поле не показывается: пустое поле и есть «по умолчанию»
        if (playlistRepository.isCustomPlaylistUrl) {
            urlView.text = playlistRepository.playlistUrl
        }

        resetButton = view.findViewById(R.id.reset)
        resetButton.setOnClickListener { urlView.text = "" }

        button = view.findViewById(R.id.button)
        button.setOnClickListener { save() }

        view.pinToBottomInsets()
    }

    private fun save() {
        hideKeyboard()
        playlistRepository.setPlaylistUrl(urlView.text)
        finish()
    }

    override fun onPause() {
        super.onPause()
        hideKeyboard()
    }

    companion object {

        fun newInstance() = TvSettingsScreen()
    }
}
