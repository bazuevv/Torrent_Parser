package com.tonapps.tonkeeper.ui.screen.music

import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.widget.Button
import androidx.appcompat.widget.AppCompatTextView
import androidx.appcompat.widget.LinearLayoutCompat
import androidx.recyclerview.widget.RecyclerView
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import com.tonapps.blockchain.model.legacy.WalletEntity
import com.tonapps.tonkeeper.extensions.isLightTheme
import com.tonapps.tonkeeper.ui.screen.main.MainScreen
import com.tonapps.tonkeeper.ui.screen.music.entity.RadioStationEntity
import com.tonapps.tonkeeper.ui.screen.music.list.Adapter
import com.tonapps.tonkeeperx.R
import com.tonapps.uikit.color.backgroundPageColor
import com.tonapps.uikit.color.backgroundTransparentColor
import com.tonapps.uikit.color.textPrimaryColor
import com.tonapps.wallet.localization.Localization
import org.koin.androidx.viewmodel.ext.android.viewModel
import uikit.drawable.BarDrawable
import uikit.extensions.collectFlow
import uikit.widget.HeaderView

/**
 * Вкладка «Музыка»: список радиостанций из каталога Radio-Browser.
 * Кошелёк экрану не нужен, но он живёт внутри MainScreen и обязан быть его Child.
 */
class MusicScreen(wallet: WalletEntity) : MainScreen.Child(R.layout.fragment_music, wallet) {

    override val fragmentName: String = "MusicScreen"

    override val viewModel: MusicViewModel by viewModel()

    private val adapter = Adapter { openStation(it) }

    private lateinit var headerView: HeaderView
    private lateinit var refreshView: SwipeRefreshLayout
    private lateinit var listView: RecyclerView
    private lateinit var placeholderView: LinearLayoutCompat
    private lateinit var placeholderTitleView: AppCompatTextView
    private lateinit var placeholderSubtitleView: AppCompatTextView
    private lateinit var placeholderButton: Button

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        headerView = view.findViewById(R.id.header)
        headerView.title = getString(Localization.music)
        headerView.setTitleGravity(Gravity.START)
        // Тот же крупный заголовок, что на «Истории» и «Платежах» (h1, 32sp).
        // Стиль задаёт только размер и шрифт, поэтому цвет возвращаем из темы
        headerView.titleView.setTextAppearance(uikit.R.style.TextAppearance_H1)
        headerView.titleView.setTextColor(requireContext().textPrimaryColor)
        headerView.hideCloseIcon()
        if (requireContext().isLightTheme) {
            headerView.setColor(requireContext().backgroundPageColor)
        } else {
            headerView.setColor(requireContext().backgroundTransparentColor)
        }

        refreshView = view.findViewById(R.id.refresh)
        refreshView.setOnRefreshListener { viewModel.refresh() }

        listView = view.findViewById(R.id.list)
        listView.adapter = adapter

        placeholderView = view.findViewById(R.id.placeholder)
        placeholderTitleView = view.findViewById(R.id.placeholder_title)
        placeholderSubtitleView = view.findViewById(R.id.placeholder_subtitle)
        placeholderButton = view.findViewById(R.id.placeholder_button)
        placeholderButton.setOnClickListener { viewModel.refresh() }

        collectFlow(viewModel.uiStateFlow) { state ->
            when (state) {
                is MusicUiState.Loading -> {
                    hidePlaceholder()
                    headerView.setSubtitle(Localization.updating)
                }
                is MusicUiState.Empty -> {
                    refreshView.isRefreshing = false
                    headerView.setSubtitle(null)
                    showPlaceholder(
                        Localization.music_stations_empty,
                        Localization.music_stations_empty_subtitle
                    )
                }
                is MusicUiState.Error -> {
                    refreshView.isRefreshing = false
                    headerView.setSubtitle(null)
                    showPlaceholder(
                        Localization.music_stations_error,
                        Localization.music_stations_error_subtitle
                    )
                }
                is MusicUiState.Items -> {
                    hidePlaceholder()
                    adapter.submitList(state.items) {
                        headerView.setSubtitle(null)
                        refreshView.isRefreshing = false
                    }
                }
            }
        }
    }

    private fun showPlaceholder(titleResId: Int, subtitleResId: Int) {
        placeholderTitleView.setText(titleResId)
        placeholderSubtitleView.setText(subtitleResId)
        placeholderView.visibility = View.VISIBLE
        listView.visibility = View.GONE
    }

    private fun hidePlaceholder() {
        placeholderView.visibility = View.GONE
        listView.visibility = View.VISIBLE
    }

    private fun openStation(station: RadioStationEntity) {
        // Плеер появится в следующей фазе
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

        fun newInstance(wallet: WalletEntity) = MusicScreen(wallet)
    }
}
