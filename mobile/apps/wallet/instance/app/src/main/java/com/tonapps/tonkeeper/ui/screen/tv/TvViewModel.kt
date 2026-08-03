package com.tonapps.tonkeeper.ui.screen.tv

import android.app.Application
import androidx.lifecycle.viewModelScope
import com.tonapps.log.L
import com.tonapps.tonkeeper.ui.base.BaseWalletVM
import com.tonapps.tonkeeper.ui.screen.tv.data.TvPlaylistRepository
import com.tonapps.tonkeeper.ui.screen.tv.entity.TvChannelEntity
import com.tonapps.tonkeeper.ui.screen.tv.list.Item
import com.tonapps.uikit.list.ListCell
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

class TvViewModel(
    app: Application,
    private val playlistRepository: TvPlaylistRepository,
) : BaseWalletVM(app) {

    private val _uiStateFlow = MutableStateFlow<TvUiState>(TvUiState.Loading)
    val uiStateFlow = _uiStateFlow.asStateFlow()

    init {
        load(forceRefresh = false)
    }

    fun refresh() {
        load(forceRefresh = true)
    }

    private fun load(forceRefresh: Boolean) {
        viewModelScope.launch {
            if (!forceRefresh) {
                _uiStateFlow.value = TvUiState.Loading
            }
            _uiStateFlow.value = try {
                buildState(playlistRepository.getChannels(forceRefresh))
            } catch (e: Throwable) {
                L.e(e, "TV playlist load failed")
                TvUiState.Error
            }
        }
    }

    private fun buildState(channels: List<TvChannelEntity>): TvUiState {
        if (channels.isEmpty()) {
            return TvUiState.Empty
        }
        val items = channels.mapIndexed { index, channel ->
            Item.Channel(
                position = ListCell.getPosition(channels.size, index),
                channel = channel,
            )
        }
        return TvUiState.Items(items)
    }
}
