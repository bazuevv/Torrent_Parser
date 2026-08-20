package com.tonapps.tonkeeper.ui.screen.music

import android.app.Application
import androidx.lifecycle.viewModelScope
import com.tonapps.log.L
import com.tonapps.tonkeeper.ui.base.BaseWalletVM
import com.tonapps.tonkeeper.ui.screen.music.data.RadioBrowserRepository
import com.tonapps.tonkeeper.ui.screen.music.entity.RadioStationEntity
import com.tonapps.tonkeeper.ui.screen.music.list.Item
import com.tonapps.uikit.list.ListCell
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

class MusicViewModel(
    app: Application,
    private val radioRepository: RadioBrowserRepository,
) : BaseWalletVM(app) {

    private val _uiStateFlow = MutableStateFlow<MusicUiState>(MusicUiState.Loading)
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
                _uiStateFlow.value = MusicUiState.Loading
            }
            _uiStateFlow.value = try {
                buildState(radioRepository.getStations(forceRefresh))
            } catch (e: Throwable) {
                L.e(e, "Radio stations load failed")
                MusicUiState.Error
            }
        }
    }

    private fun buildState(stations: List<RadioStationEntity>): MusicUiState {
        if (stations.isEmpty()) {
            return MusicUiState.Empty
        }
        val items = stations.mapIndexed { index, station ->
            Item.Station(
                position = ListCell.getPosition(stations.size, index),
                station = station,
            )
        }
        return MusicUiState.Items(items)
    }
}
