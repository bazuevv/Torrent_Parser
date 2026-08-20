package com.tonapps.tonkeeper.ui.screen.music

import android.os.Bundle
import android.view.Gravity
import android.view.View
import androidx.core.view.doOnLayout
import androidx.core.view.updatePadding
import androidx.core.widget.NestedScrollView
import androidx.recyclerview.widget.RecyclerView
import com.tonapps.blockchain.model.legacy.WalletEntity
import com.tonapps.tonkeeper.extensions.isLightTheme
import com.tonapps.tonkeeper.ui.base.BaseWalletVM
import com.tonapps.tonkeeper.ui.screen.main.MainScreen
import com.tonapps.tonkeeperx.R
import com.tonapps.uikit.color.backgroundPageColor
import com.tonapps.uikit.color.backgroundTransparentColor
import com.tonapps.uikit.color.textPrimaryColor
import com.tonapps.wallet.localization.Localization
import uikit.drawable.BarDrawable
import uikit.widget.HeaderView

/**
 * Каркас вкладки «Музыка»: заголовок и заглушка. Список радиостанций
 * из Radio-Browser и плеер появятся в следующих фазах.
 */
class MusicScreen(wallet: WalletEntity) : MainScreen.Child(R.layout.fragment_music, wallet) {

    override val fragmentName: String = "MusicScreen"

    override val viewModel: BaseWalletVM? = null

    private lateinit var headerView: HeaderView
    private lateinit var contentView: NestedScrollView

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        headerView = view.findViewById(R.id.header)
        contentView = view.findViewById(R.id.content)

        // Шапка лежит поверх контента, и её высота = barHeight + вырез статус-бара,
        // поэтому отступ берём фактический, а не из константы
        headerView.doOnLayout { contentView.updatePadding(top = it.measuredHeight) }
        headerView.title = getString(Localization.music)
        headerView.setTitleGravity(Gravity.START)
        // Тот же размер, что у крупного заголовка «История» (MoonTopAppBarLarge — h1, 32sp).
        // Стиль задаёт только размер и шрифт, поэтому цвет возвращаем из темы
        headerView.titleView.setTextAppearance(uikit.R.style.TextAppearance_H1)
        headerView.titleView.setTextColor(requireContext().textPrimaryColor)
        headerView.hideCloseIcon()
        if (requireContext().isLightTheme) {
            headerView.setColor(requireContext().backgroundPageColor)
        } else {
            headerView.setColor(requireContext().backgroundTransparentColor)
        }
    }

    // Списка на экране пока нет — прокручивать и затемнять шапку нечего
    override fun getRecyclerView(): RecyclerView? = null

    override fun getTopBarDrawable(): BarDrawable? = headerView.background as? BarDrawable

    companion object {

        fun newInstance(wallet: WalletEntity) = MusicScreen(wallet)
    }
}
