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

    private var stations: List<RadioStationEntity> = emptyList()
    private var query: String = ""

    /** Полный список без поискового фильтра — плейлист переключения станций */
    val allStations: List<RadioStationEntity>
        get() = stations

    init {
        load(forceRefresh = false)
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
                _uiStateFlow.value = MusicUiState.Loading
            }
            _uiStateFlow.value = try {
                stations = radioRepository.getStations(forceRefresh)
                buildState()
            } catch (e: Throwable) {
                L.e(e, "Radio stations load failed")
                stations = emptyList()
                MusicUiState.Error
            }
        }
    }

    private fun buildState(): MusicUiState {
        if (stations.isEmpty()) {
            return MusicUiState.Empty
        }
        val filtered = if (query.isEmpty()) {
            stations
        } else {
            stations.filter { it.name.contains(query, ignoreCase = true) }
        }
        if (filtered.isEmpty()) {
            return MusicUiState.NotFound
        }
        val items = filtered.mapIndexed { index, station ->
            Item.Station(
                position = ListCell.getPosition(filtered.size, index),
                station = station,
            )
        }
        return MusicUiState.Items(items)
    }
}
