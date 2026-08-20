package com.tonapps.tonkeeper.ui.screen.music.player

import android.content.ComponentName
import android.os.Bundle
import android.view.View
import android.widget.Button
import androidx.appcompat.widget.AppCompatImageView
import androidx.appcompat.widget.AppCompatTextView
import androidx.core.content.ContextCompat
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import com.tonapps.extensions.getParcelableCompat
import com.tonapps.log.L
import com.tonapps.tonkeeper.ui.base.BaseWalletScreen
import com.tonapps.tonkeeper.ui.base.BaseWalletVM
import com.tonapps.tonkeeper.ui.base.ScreenContext
import com.tonapps.tonkeeper.ui.screen.music.entity.RadioStationEntity
import com.tonapps.tonkeeper.ui.screen.music.playback.RadioPlaybackService
import com.tonapps.tonkeeperx.R
import uikit.base.BaseFragment
import uikit.widget.AsyncImageView
import uikit.widget.HeaderView
import uikit.widget.LoaderView

/**
 * Экран одной радиостанции. Плеер живёт в RadioPlaybackService, экран лишь
 * подключается к нему контроллером: закрытие экрана не останавливает эфир —
 * он играет фоном под уведомлением. Живой поток без позиции, поэтому
 * интерфейс — только play/pause.
 */
class RadioPlayerScreen : BaseWalletScreen<ScreenContext.None>(
    R.layout.fragment_radio_player,
    ScreenContext.None
), BaseFragment.SwipeBack {

    override val fragmentName: String = "RadioPlayerScreen"

    override val viewModel: BaseWalletVM? = null

    private val station: RadioStationEntity by lazy {
        requireArguments().getParcelableCompat(ARG_STATION)!!
    }

    private var pendingFuture: com.google.common.util.concurrent.ListenableFuture<MediaController>? = null

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

        override fun onPlayerError(error: PlaybackException) {
            L.e(error, "Radio playback error: ${station.name}")
            showError()
        }
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        headerView = view.findViewById(R.id.header)
        headerView.title = station.name
        headerView.doOnCloseClick = { finish() }

        logoView = view.findViewById(R.id.logo)
        logoPlaceholderView = view.findViewById(R.id.logo_placeholder)
        nameView = view.findViewById(R.id.name)
        nameView.text = station.name

        infoView = view.findViewById(R.id.info)
        infoView.text = station.info
        infoView.visibility = if (station.info.isNullOrBlank()) View.GONE else View.VISIBLE

        bindLogo()

        loaderView = view.findViewById(R.id.loader)
        playButtonView = view.findViewById(R.id.play_button)
        playButtonView.setOnClickListener { togglePlay() }

        errorView = view.findViewById(R.id.error)
        errorButtonView = view.findViewById(R.id.error_button)
        errorButtonView.setOnClickListener { retry() }

        connectController()
    }

    private fun bindLogo() {
        val url = station.logoUrl
        if (url.isNullOrBlank()) {
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
                applyPlayIcon(mediaController.isPlaying)
                if (pendingPlay) {
                    pendingPlay = false
                    startPlayback()
                }
            } catch (e: Throwable) {
                L.e(e, "Radio controller connect failed")
                showError()
            }
        }, ContextCompat.getMainExecutor(requireContext()))
        pendingFuture = future
    }

    private fun startPlayback() {
        val controller = controller ?: return
        // Метаданные попадают в уведомление: название станции, кодек и обложка
        val metadata = MediaMetadata.Builder()
            .setTitle(station.name)
            .setArtist(station.info)
            .setArtworkUri(station.logoUrl?.let { android.net.Uri.parse(it) })
            .build()
        controller.setMediaItem(MediaItem.Builder().setUri(station.url).setMediaMetadata(metadata).build())
        controller.prepare()
        controller.play()
    }

    private fun togglePlay() {
        val controller = controller ?: run {
            pendingPlay = true
            return
        }
        if (controller.isPlaying) {
            controller.pause()
        } else {
            if (controller.playbackState == Player.STATE_IDLE &&
                controller.currentMediaItem?.mediaMetadata?.title != station.name
            ) {
                // В сессии лежит другая станция или ничего — переключаем на эту
                startPlayback()
                return
            }
            if (controller.playbackState == Player.STATE_IDLE) {
                retry()
                return
            }
            controller.play()
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

        private const val ARG_STATION = "station"

        fun newInstance(station: RadioStationEntity): RadioPlayerScreen {
            val screen = RadioPlayerScreen()
            screen.putParcelableArg(ARG_STATION, station)
            return screen
        }
    }
}
