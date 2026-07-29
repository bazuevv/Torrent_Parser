package com.tonapps.tonkeeper.ui.screen.name.edit

import android.app.Dialog
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.view.View
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.lifecycle.lifecycleScope
import com.tonapps.tonkeeper.koin.walletViewModel
import com.tonapps.tonkeeper.ui.base.WalletContextScreen
import com.tonapps.tonkeeper.ui.component.label.LabelEditorView
import com.tonapps.tonkeeper.ui.component.label.PhotoCropView
import com.tonapps.wallet.data.settings.WalletAvatarPhotoStore
import com.tonapps.tonkeeperx.R
import com.tonapps.blockchain.model.legacy.WalletEntity
import kotlinx.coroutines.launch
import uikit.base.BaseFragment
import uikit.extensions.doKeyboardAnimation
import uikit.widget.HeaderView

class EditNameScreen(wallet: WalletEntity): WalletContextScreen(R.layout.fragment_name_edit, wallet), BaseFragment.BottomSheet {

    override val fragmentName: String = "EditNameScreen"

    override val viewModel: EditNameViewModel by walletViewModel()

    private lateinit var editorView: LabelEditorView

    // Системный пикер: доступ к галерее целиком не запрашивается, пользователь
    // сам отдаёт приложению одну картинку, поэтому разрешений не требуется.
    private val pickPhoto = registerForActivityResult(
        ActivityResultContracts.PickVisualMedia()
    ) { uri: Uri? ->
        uri ?: return@registerForActivityResult
        viewModel.decodePhoto(uri) { bitmap ->
            bitmap ?: return@decodePhoto
            showCropDialog(bitmap)
        }
    }

    /**
     * Кадрирование показывается поверх редактора обычным диалогом, а не отдельным
     * экраном навигации: результат нужен здесь же, и так его не приходится
     * прокидывать между экранами.
     */
    private fun showCropDialog(bitmap: Bitmap) {
        val context = context ?: return
        val dialog = Dialog(context, android.R.style.Theme_Black_NoTitleBar_Fullscreen)
        val content = layoutInflater.inflate(R.layout.dialog_photo_crop, null)
        val cropView = content.findViewById<PhotoCropView>(R.id.crop_view)
        cropView.setBitmap(bitmap)
        content.findViewById<View>(R.id.crop_cancel).setOnClickListener { dialog.dismiss() }
        content.findViewById<View>(R.id.crop_done).setOnClickListener {
            val cropped = cropView.crop(WalletAvatarPhotoStore.OUTPUT_SIZE)
            dialog.dismiss()
            cropped ?: return@setOnClickListener
            viewModel.setPhoto(cropped) { path ->
                editorView.photoPath = path
            }
        }
        dialog.setContentView(content)
        dialog.show()
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        val headerView = view.findViewById<HeaderView>(R.id.header)
        headerView.doOnActionClick = { finish() }

        editorView = view.findViewById(R.id.editor)
        editorView.doOnDone = ::saveLabel
        editorView.name = screenContext.wallet.label.name
        editorView.emoji = screenContext.wallet.label.emoji
        editorView.color = screenContext.wallet.label.color
        editorView.photoEnabled = true
        editorView.photoPath = viewModel.photoPath
        editorView.doOnPickPhoto = {
            pickPhoto.launch(
                PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)
            )
        }
        editorView.doOnRemovePhoto = {
            viewModel.removePhoto()
            editorView.photoPath = null
        }

        view.doKeyboardAnimation { offset, progress, showKeyboard ->
            editorView.setBottomOffset(offset, progress)
        }
    }

    override fun onResume() {
        super.onResume()
        lifecycleScope.launch { editorView.loadEmoji() }
    }

    override fun onPause() {
        viewModel.save(editorView.name, editorView.emoji, editorView.color)
        super.onPause()
    }

    private fun saveLabel(name: String, emoji: String, color: Int) {
        viewModel.save(name, emoji, color)
        finish()
    }

    override fun onDragging() {
        super.onDragging()
        editorView.removeFocus()
    }

    companion object {

        fun newInstance(wallet: WalletEntity) = EditNameScreen(wallet)
    }
}