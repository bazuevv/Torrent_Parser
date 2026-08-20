package com.tonapps.tonkeeper.ui.screen.music.player

import android.content.ComponentName
import android.os.Bundle
import android.view.View
import android.widget.Button
import androidx.appcompat.widget.AppCompatImageView
import androidx.appcompat.widget.AppCompatTextView
import androidx.core.content.ContextCompat
import androidx.core.os.BundleCompat
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import com.tonapps.log.L
import com.tonapps.tonkeeper.ui.base.BaseWalletScreen
import com.tonapps.tonkeeper.ui.base.BaseWalletVM
import com.tonapps.tonkeeper.ui.base.ScreenContext
import com.tonapps.tonkeeper.ui.screen.music.entity.RadioStationEntity
import com.tonapps.tonkeeper.ui.screen.music.playback.RadioPlaybackService
import com.tonapps.tonkeeperx.R
import com.google.common.util.concurrent.ListenableFuture
import uikit.base.BaseFragment
import uikit.widget.AsyncImageView
import uikit.widget.HeaderView
import uikit.widget.LoaderView

/**
 * Экран радиостанции. Плеер живёт в RadioPlaybackService и держит плейлист
 * всех станций: кнопки «назад/вперёд» в уведомлении переключают их, а экран
 * через onMediaItemTransition следует за переключением. Открытие экрана
 * настраивает эфир на выбранную станцию; закрытие не останавливает звук.
 */
class RadioPlayerScreen : BaseWalletScreen<ScreenContext.None>(
    R.layout.fragment_radio_player,
    ScreenContext.None
), BaseFragment.SwipeBack {

    override val fragmentName: String = "RadioPlayerScreen"

    override val viewModel: BaseWalletVM? = null

    private val stations: List<RadioStationEntity> by lazy {
        BundleCompat.getParcelableArrayList(
            requireArguments(), ARG_STATIONS, RadioStationEntity::class.java
        ).orEmpty()
    }

    private val station: RadioStationEntity by lazy {
        val index = openedIndex
        if (index in stations.indices) stations[index] else stations.first()
    }

    private val openedIndex: Int
        get() = requireArguments().getInt(ARG_INDEX, 0)

    // Станция, которую экран показывает в данный момент — меняется, когда
    // пользователь переключает эфир кнопками в уведомлении
    private var shownStation: RadioStationEntity? = null

    private var pendingFuture: ListenableFuture<MediaController>? = null

    private var controller: MediaController? = null

    // Пользователь нажал play до того, как контроллер подключился к сервису
    private var pendingPlay = false

    private lateinit var headerView: HeaderView
    private lateinit var logoView: AsyncImageView
    private lateinit var logoPlaceholderView: AppCompatImageView
    private lateinit var nameView: AppCompatTextView
    private lateinit var infoView: AppCompatTextView
    private lateinit var loaderView: LoaderView
    private lateinit var playButtonView: AppCompatImageView
    private lateinit var errorView: AppCompatTextView
    private lateinit var errorButtonView: Button

    private val playerListener = object : Player.Listener {

        override fun onPlaybackStateChanged(playbackState: Int) {
            loaderView.visibility = if (playbackState == Player.STATE_BUFFERING) {
                View.VISIBLE
            } else {
                View.GONE
            }
            if (playbackState == Player.STATE_READY) {
                hideError()
            }
        }

        override fun onIsPlayingChanged(isPlaying: Boolean) {
            applyPlayIcon(isPlaying)
        }

        override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
            // Станцию переключили из уведомления — показываем новую
            val url = mediaItem?.mediaId ?: return
            val next = stations.firstOrNull { it.url == url } ?: return
            applyStation(next)
        }

        override fun onPlayerError(error: PlaybackException) {
            L.e(error, "Radio playback error: ${station.name}")
            showError()
        }
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        headerView = view.findViewById(R.id.header)
        headerView.doOnCloseClick = { finish() }

        logoView = view.findViewById(R.id.logo)
        logoPlaceholderView = view.findViewById(R.id.logo_placeholder)
        nameView = view.findViewById(R.id.name)
        infoView = view.findViewById(R.id.info)

        loaderView = view.findViewById(R.id.loader)
        playButtonView = view.findViewById(R.id.play_button)
        playButtonView.setOnClickListener { togglePlay() }

        errorView = view.findViewById(R.id.error)
        errorButtonView = view.findViewById(R.id.error_button)
        errorButtonView.setOnClickListener { retry() }

        applyStation(station)
        connectController()
    }

    private fun applyStation(station: RadioStationEntity) {
        if (shownStation?.url == station.url) {
            return
        }
        shownStation = station
        headerView.title = station.name
        nameView.text = station.name
        infoView.text = station.info
        infoView.visibility = if (station.info.isNullOrBlank()) View.GONE else View.VISIBLE
        bindLogo(station.logoUrl)
    }

    private fun bindLogo(url: String?) {
        if (url.isNullOrBlank()) {
            logoView.visibility = View.GONE
            logoPlaceholderView.visibility = View.VISIBLE
            return
        }
        logoView.visibility = View.VISIBLE
        logoPlaceholderView.visibility = View.GONE
        logoView.setImageURI(url, null)
    }

    private fun connectController() {
        // Ошибка подключения к сервису не должна ронять приложение — она
        // превращается в экран «Повторить», как и ошибка самого потока
        val future = try {
            MediaController.Builder(
                requireContext(),
                SessionToken(requireContext(), ComponentName(requireContext(), RadioPlaybackService::class.java))
            ).buildAsync()
        } catch (e: Throwable) {
            L.e(e, "Radio session token failed")
            showError()
            return
        }
        future.addListener({
            try {
                val mediaController = future.get()
                mediaController.addListener(playerListener)
                controller = mediaController
                if (mediaController.currentMediaItem?.mediaId != station.url) {
                    // В сессии другая станция или ничего — настраиваем эфир на эту
                    startPlayback()
                } else {
                    applyPlayIcon(mediaController.isPlaying)
                    if (pendingPlay) {
                        pendingPlay = false
                        mediaController.play()
                    }
                }
            } catch (e: Throwable) {
                L.e(e, "Radio controller connect failed")
                showError()
            }
        }, ContextCompat.getMainExecutor(requireContext()))
        pendingFuture = future
    }

    // C.TIME_UNSET помечен @UnstableApi, хотя значение «позиция по умолчанию»
    // для живого потока стабильно уже много лет
    @androidx.annotation.OptIn(UnstableApi::class)
    private fun startPlayback() {
        val controller = controller ?: return
        val index = openedIndex.coerceIn(0, stations.lastIndex)
        controller.setMediaItems(stations.map { it.toMediaItem() }, index, C.TIME_UNSET)
        controller.prepare()
        controller.play()
    }

    private fun RadioStationEntity.toMediaItem(): MediaItem {
        // Метаданные попадают в уведомление: название станции, кодек и обложка.
        // mediaId = URL потока, по нему экран опознаёт текущую станцию
        val metadata = MediaMetadata.Builder()
            .setTitle(name)
            .setArtist(info)
            .setArtworkUri(logoUrl?.let { android.net.Uri.parse(it) })
            .build()
        return MediaItem.Builder()
            .setUri(url)
            .setMediaId(url)
            .setMediaMetadata(metadata)
            .build()
    }

    private fun togglePlay() {
        val controller = controller ?: run {
            pendingPlay = true
            return
        }
        when {
            controller.isPlaying -> controller.pause()
            controller.playbackState == Player.STATE_IDLE -> retry()
            else -> controller.play()
        }
    }

    private fun retry() {
        hideError()
        controller?.let {
            it.seekToDefaultPosition()
            it.prepare()
            it.play()
        }
    }

    private fun applyPlayIcon(isPlaying: Boolean) {
        playButtonView.setImageResource(
            if (isPlaying) R.drawable.ic_radio_pause_28 else R.drawable.ic_radio_play_28
        )
    }

    private fun showError() {
        errorView.visibility = View.VISIBLE
        errorButtonView.visibility = View.VISIBLE
        loaderView.visibility = View.GONE
        applyPlayIcon(false)
    }

    private fun hideError() {
        errorView.visibility = View.GONE
        errorButtonView.visibility = View.GONE
    }

    override fun onDestroyView() {
        // Отпускаем только контроллер: сам плеер в сервисе продолжает играть фоном
        controller?.removeListener(playerListener)
        controller?.release()
        controller = null
        pendingFuture?.cancel(true)
        pendingFuture = null
        super.onDestroyView()
    }

    companion object {

        private const val ARG_STATIONS = "stations"
        private const val ARG_INDEX = "index"

        fun newInstance(stations: ArrayList<RadioStationEntity>, index: Int): RadioPlayerScreen {
            val screen = RadioPlayerScreen()
            screen.putParcelableArrayListArg(ARG_STATIONS, stations)
            screen.putIntArg(ARG_INDEX, index)
            return screen
        }
    }
}
