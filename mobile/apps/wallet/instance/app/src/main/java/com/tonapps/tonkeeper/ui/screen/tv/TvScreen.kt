package com.tonapps.tonkeeper.ui.screen.tv

import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.widget.Button
import androidx.appcompat.widget.AppCompatTextView
import androidx.appcompat.widget.LinearLayoutCompat
import androidx.core.view.doOnLayout
import androidx.recyclerview.widget.RecyclerView
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import com.tonapps.blockchain.model.legacy.WalletEntity
import com.tonapps.tonkeeper.extensions.isLightTheme
import com.tonapps.tonkeeper.ui.screen.main.MainScreen
import com.tonapps.tonkeeper.ui.screen.tv.entity.TvChannelEntity
import com.tonapps.tonkeeper.ui.screen.tv.list.Adapter
import com.tonapps.tonkeeper.ui.screen.tv.player.TvPlayerScreen
import com.tonapps.tonkeeper.ui.screen.tv.settings.TvSettingsScreen
import com.tonapps.tonkeeperx.R
import com.tonapps.uikit.color.backgroundPageColor
import com.tonapps.uikit.color.backgroundTransparentColor
import com.tonapps.uikit.color.textPrimaryColor
import com.tonapps.uikit.icon.UIKitIcon
import com.tonapps.wallet.localization.Localization
import org.koin.androidx.viewmodel.ext.android.viewModel
import uikit.drawable.BarDrawable
import uikit.extensions.collectFlow
import uikit.widget.HeaderView
import uikit.widget.SearchInput

/**
 * Вкладка «ТВ»: список каналов из M3U-плейлиста. Кошелёк экрану не нужен,
 * но он живёт внутри MainScreen и обязан быть его Child.
 */
class TvScreen(wallet: WalletEntity) : MainScreen.Child(R.layout.fragment_tv, wallet) {

    override val fragmentName: String = "TvScreen"

    override val viewModel: TvViewModel by viewModel()

    private val adapter = Adapter { openChannel(it) }

    private lateinit var headerView: HeaderView
    private lateinit var refreshView: SwipeRefreshLayout
    private lateinit var listView: RecyclerView
    private lateinit var searchView: SearchInput
    private lateinit var placeholderView: LinearLayoutCompat
    private lateinit var placeholderTitleView: AppCompatTextView
    private lateinit var placeholderSubtitleView: AppCompatTextView
    private lateinit var placeholderButton: Button

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        headerView = view.findViewById(R.id.header)
        headerView.title = getString(Localization.tv)
        headerView.setTitleGravity(Gravity.START)
        // Тот же крупный заголовок, что на «Истории» и «Платежах» (h1, 32sp).
        // Стиль задаёт только размер и шрифт, поэтому цвет возвращаем из темы
        headerView.titleView.setTextAppearance(uikit.R.style.TextAppearance_H1)
        headerView.titleView.setTextColor(requireContext().textPrimaryColor)
        headerView.hideCloseIcon()
        headerView.setAction(UIKitIcon.ic_gear_28)
        headerView.doOnActionClick = { navigation?.add(TvSettingsScreen.newInstance()) }
        if (requireContext().isLightTheme) {
            headerView.setColor(requireContext().backgroundPageColor)
        } else {
            headerView.setColor(requireContext().backgroundTransparentColor)
        }

        refreshView = view.findViewById(R.id.refresh)
        refreshView.setOnRefreshListener { viewModel.refresh() }

        listView = view.findViewById(R.id.list)
        listView.adapter = adapter

        searchView = view.findViewById(R.id.search)
        searchView.doOnTextChanged = { viewModel.setQuery(it?.toString()) }
        // Поиск закреплён под шапкой, а её высота = barHeight + вырез статус-бара,
        // поэтому отступ берём фактический, а не из константы
        headerView.doOnLayout { searchView.translationY = it.measuredHeight.toFloat() }

        placeholderView = view.findViewById(R.id.placeholder)
        placeholderTitleView = view.findViewById(R.id.placeholder_title)
        placeholderSubtitleView = view.findViewById(R.id.placeholder_subtitle)
        placeholderButton = view.findViewById(R.id.placeholder_button)
        placeholderButton.setOnClickListener { viewModel.refresh() }

        collectFlow(viewModel.uiStateFlow) { state ->
            when (state) {
                is TvUiState.Loading -> {
                    hidePlaceholder()
                    headerView.setSubtitle(Localization.updating)
                }
                is TvUiState.Empty -> {
                    refreshView.isRefreshing = false
                    headerView.setSubtitle(null)
                    showPlaceholder(Localization.tv_channels_empty, Localization.tv_channels_empty_subtitle)
                }
                is TvUiState.NotFound -> {
                    refreshView.isRefreshing = false
                    headerView.setSubtitle(null)
                    showPlaceholder(
                        titleResId = Localization.tv_channels_not_found,
                        subtitleResId = Localization.tv_channels_not_found_subtitle,
                        withRetry = false,
                    )
                }
                is TvUiState.Error -> {
                    refreshView.isRefreshing = false
                    headerView.setSubtitle(null)
                    showPlaceholder(Localization.tv_channels_error, Localization.tv_channels_error_subtitle)
                }
                is TvUiState.Items -> {
                    hidePlaceholder()
                    adapter.submitList(state.items) {
                        headerView.setSubtitle(null)
                        refreshView.isRefreshing = false
                    }
                }
            }
        }
    }

    private fun showPlaceholder(titleResId: Int, subtitleResId: Int, withRetry: Boolean = true) {
        placeholderTitleView.setText(titleResId)
        placeholderSubtitleView.setText(subtitleResId)
        // По пустому поиску обновлять нечего — там помогает только другой запрос
        placeholderButton.visibility = if (withRetry) View.VISIBLE else View.GONE
        placeholderView.visibility = View.VISIBLE
        listView.visibility = View.GONE
    }

    private fun hidePlaceholder() {
        placeholderView.visibility = View.GONE
        listView.visibility = View.VISIBLE
    }

    private fun openChannel(channel: TvChannelEntity) {
        navigation?.add(TvPlayerScreen.newInstance(channel))
    }

    override fun getRecyclerView(): RecyclerView? {
        if (this::listView.isInitialized) {
            return listView
        }
        return null
    }

    override fun getTopBarDrawable(): BarDrawable? {
        if (this::headerView.isInitialized) {
            return headerView.background as? BarDrawable
        }
        return null
    }

    companion object {

        fun newInstance(wallet: WalletEntity) = TvScreen(wallet)
    }
}
