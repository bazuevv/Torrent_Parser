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
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.launch

class TvViewModel(
    app: Application,
    private val playlistRepository: TvPlaylistRepository,
) : BaseWalletVM(app) {

    private val _uiStateFlow = MutableStateFlow<TvUiState>(TvUiState.Loading)
    val uiStateFlow = _uiStateFlow.asStateFlow()

    private var channels: List<TvChannelEntity> = emptyList()
    private var query: String = ""

    init {
        load(forceRefresh = false)
        // Смена адреса плейлиста сбрасывает кэш в репозитории, поэтому здесь
        // достаточно обычной перезагрузки — сеть дёрнется сама
        playlistRepository.playlistUrlFlow.drop(1).collectFlow { load(forceRefresh = false) }
    }

    fun refresh() {
        load(forceRefresh = true)
    }

    fun setQuery(text: String?) {
        val value = text?.trim().orEmpty()
        if (value.equals(query, ignoreCase = true)) {
            return
        }
        query = value
        _uiStateFlow.value = buildState()
    }

    private fun load(forceRefresh: Boolean) {
        viewModelScope.launch {
            if (!forceRefresh) {
                _uiStateFlow.value = TvUiState.Loading
            }
            _uiStateFlow.value = try {
                channels = playlistRepository.getChannels(forceRefresh)
                buildState()
            } catch (e: Throwable) {
                L.e(e, "TV playlist load failed")
                channels = emptyList()
                TvUiState.Error
            }
        }
    }

    private fun buildState(): TvUiState {
        if (channels.isEmpty()) {
            return TvUiState.Empty
        }
        val filtered = if (query.isEmpty()) {
            channels
        } else {
            channels.filter { it.name.contains(query, ignoreCase = true) }
        }
        if (filtered.isEmpty()) {
            return TvUiState.NotFound
        }
        val items = filtered.mapIndexed { index, channel ->
            Item.Channel(
                position = ListCell.getPosition(filtered.size, index),
                channel = channel,
            )
        }
        return TvUiState.Items(items)
    }
}
