package com.tonapps.tonkeeper.ui.screen.send.contacts.add

import android.Manifest
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.provider.ContactsContract
import android.view.View
import android.widget.Button
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.widget.AppCompatTextView
import androidx.core.content.ContextCompat
import androidx.core.net.toUri
import com.tonapps.tonkeeper.extensions.hideKeyboard
import com.tonapps.tonkeeper.extensions.toast
import com.tonapps.uikit.color.accentBlueColor
import com.tonapps.wallet.localization.Localization
import com.tonapps.tonkeeper.koin.walletViewModel
import com.tonapps.tonkeeper.ui.base.WalletContextScreen
import com.tonapps.tonkeeper.ui.screen.camera.CameraMode
import com.tonapps.tonkeeper.ui.screen.camera.CameraScreen
import com.tonapps.tonkeeperx.R
import com.tonapps.blockchain.model.legacy.WalletEntity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import uikit.base.BaseFragment
import uikit.dialog.alert.AlertDialog
import uikit.extensions.collectFlow
import uikit.widget.AsyncImageView
import java.io.File
import java.util.UUID
import uikit.extensions.pinToBottomInsets
import uikit.widget.InputView
import uikit.widget.ModalHeader

class AddContactScreen(wallet: WalletEntity): WalletContextScreen(R.layout.fragment_contact_add, wallet), BaseFragment.Modal {

    override val fragmentName: String = "AddContactScreen"

    override val viewModel: AddContactViewModel by walletViewModel()

    private lateinit var headerView: ModalHeader
    private lateinit var nameView: InputView
    private lateinit var addressView: InputView
    private lateinit var button: Button
    private lateinit var createPhoneContactButton: Button
    private lateinit var photoView: AsyncImageView
    private lateinit var addressDuplicateView: AppCompatTextView

    /**
     * Системный выбор контакта: пользователь сам указывает, кого отдать приложению,
     * поэтому разрешение READ_CONTACTS не нужно — доступ выдаётся к одному URI.
     */
    private val pickContact = registerForActivityResult(ActivityResultContracts.PickContact()) { uri ->
        uri?.let { applyPickedContact(it) }
    }

    /**
     * Фото контакта лежит по вложенному адресу …/display_photo, и доступ от системного
     * выбора на него не распространяется — провайдер требует READ_CONTACTS. Поэтому
     * разрешение спрашивается только когда пользователь сам выбрал вариант с фото.
     */
    private val requestContactsPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        withPhoto = granted
        pickContact.launch(null)
    }

    private var withPhoto: Boolean = false

    // Уникальный ключ на экземпляр экрана: два открытых окна добавления не должны
    // получать чужой результат сканирования
    private val scannerRequestKey: String by lazy { "contact_scanner_${UUID.randomUUID()}" }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        headerView = view.findViewById(R.id.header)
        headerView.onCloseClick = { finish() }

        nameView = view.findViewById(R.id.name)
        nameView.doOnTextChange = { viewModel.setName(it) }
        nameView.doOnIconClick = { pickContactOrAsk() }
        requireArguments().getString(ARG_NAME)?.let { nameView.text = it }

        photoView = view.findViewById(R.id.photo)

        createPhoneContactButton = view.findViewById(R.id.create_phone_contact)
        createPhoneContactButton.setOnClickListener { createPhoneContact() }

        addressView = view.findViewById(R.id.address)
        addressView.doOnTextChange = { viewModel.setAddress(it) }
        addressView.doOnIconClick = {
            hideKeyboard()
            navigation?.add(CameraScreen.newInstance(CameraMode.Result(scannerRequestKey)))
        }

        requireArguments().getString(ARG_ADDRESS)?.let { addressView.text = it }

        button = view.findViewById(R.id.button)
        button.setOnClickListener {
            hideKeyboard()
            viewModel.save()
        }

        addressDuplicateView = view.findViewById(R.id.address_duplicate)

        navigation?.setFragmentResultListener(scannerRequestKey) { bundle ->
            val address = bundle.getString(CameraScreen.ARG_RESULT_ADDRESS)
                ?: return@setFragmentResultListener
            addressView.text = address
        }

        collectFlow(viewModel.accountFlow, ::applyAccountState)
        collectFlow(viewModel.duplicateContactFlow, ::applyDuplicateState)
        collectFlow(viewModel.isEnabledButtonFlow) { button.isEnabled = it }

        view.pinToBottomInsets()
    }

    override fun onResume() {
        super.onResume()
        nameView.focus()
    }

    override fun onPause() {
        super.onPause()
        hideKeyboard()
    }
    
    /**
     * Диалог нужен только чтобы решить, спрашивать ли разрешение. Если доступ уже выдан,
     * фото копируется без вопросов — переспрашивать пользователя не о чем.
     */
    private fun pickContactOrAsk() {
        if (hasContactsPermission()) {
            withPhoto = true
            pickContact.launch(null)
        } else {
            showPickContactDialog()
        }
    }

    private fun hasContactsPermission(): Boolean {
        return ContextCompat.checkSelfPermission(
            requireContext(),
            Manifest.permission.READ_CONTACTS
        ) == PackageManager.PERMISSION_GRANTED
    }

    private fun showPickContactDialog() {
        val builder = AlertDialog.Builder(requireContext())
        builder.setTitle(Localization.pick_contact_title)
        // Вопрос идёт после пояснения — на него отвечают кнопки «Да» и «Нет».
        // Отдельного слота под такой текст у диалога нет, поэтому он в сообщении
        builder.setMessage(
            "${getString(Localization.pick_contact_message)}\n\n" +
                    getString(Localization.pick_contact_question)
        )
        builder.setPositiveButton(Localization.pick_contact_plain) { dialog ->
            dialog.dismiss()
            withPhoto = false
            pickContact.launch(null)
        }
        builder.setNegativeButton(
            resId = Localization.pick_contact_with_photo,
            color = requireContext().accentBlueColor
        ) { dialog ->
            dialog.dismiss()
            requestContactsPermission.launch(Manifest.permission.READ_CONTACTS)
        }
        builder.show()
    }

    private fun applyPickedContact(uri: Uri) {
        val contact = requireContext().contentResolver.query(
            uri,
            arrayOf(
                ContactsContract.Contacts.DISPLAY_NAME,
                ContactsContract.Contacts.LOOKUP_KEY,
                ContactsContract.Contacts.PHOTO_URI
            ),
            null,
            null,
            null
        )?.use { cursor ->
            if (!cursor.moveToFirst()) {
                return@use null
            }
            PhoneContact(
                name = cursor.getString(0),
                lookupKey = cursor.getString(1),
                photoUri = cursor.getString(2)
            )
        } ?: return

        if (contact.name.isNullOrBlank()) {
            return
        }

        nameView.text = contact.name
        nameView.focus()

        lifecycleScope.launch {
            // Фото копируется сразу после выбора: доступ к телефонной книге живёт,
            // пока не закрыт экран, и к моменту сохранения контакта может истечь
            val photoPath = if (withPhoto) {
                withContext(Dispatchers.IO) { copyContactPhoto(uri) }
            } else {
                null
            }
            contact.lookupKey?.let { viewModel.setPhoneContact(contact.name, it, photoPath) }
            applyContactPhoto(photoPath)
        }
    }

    /**
     * Читает фото штатным openContactPhotoInputStream: временный доступ выдаётся
     * на URI контакта, а PHOTO_URI — отдельный адрес, напрямую его не открыть.
     */
    private fun copyContactPhoto(contactUri: Uri): String? {
        return try {
            val input = ContactsContract.Contacts.openContactPhotoInputStream(
                requireContext().contentResolver,
                contactUri,
                true
            ) ?: return null

            val dir = File(requireContext().filesDir, PHOTOS_DIR).apply { mkdirs() }
            val file = File(dir, "${UUID.randomUUID()}.jpg")
            input.use { stream ->
                file.outputStream().use { output -> stream.copyTo(output) }
            }
            file.absolutePath
        } catch (ignored: Throwable) {
            null
        }
    }

    private fun applyContactPhoto(photoPath: String?) {
        // У контакта может не быть фото — тогда прячем аватар, а не показываем пустоту
        if (photoPath.isNullOrBlank()) {
            photoView.visibility = View.GONE
            return
        }
        photoView.visibility = View.VISIBLE
        photoView.setImageURI(File(photoPath).toUri())
    }

    private data class PhoneContact(
        val name: String?,
        val lookupKey: String?,
        val photoUri: String?
    )

    private fun createPhoneContact() {
        val intent = Intent(Intent.ACTION_INSERT).apply {
            type = ContactsContract.Contacts.CONTENT_TYPE
        }
        try {
            startActivity(intent)
        } catch (ignored: ActivityNotFoundException) {
            navigation?.toast(Localization.phone_contacts_unavailable)
        }
    }

    /**
     * Предупреждение, а не блокировка: сохранить второй контакт с тем же адресом
     * по-прежнему можно — иногда это осознанно (например, разные пометки).
     */
    private fun applyDuplicateState(contactName: String?) {
        if (contactName.isNullOrBlank()) {
            addressDuplicateView.visibility = View.GONE
            return
        }
        addressDuplicateView.visibility = View.VISIBLE
        addressDuplicateView.text = getString(Localization.contact_address_duplicate, contactName)
    }

    private fun applyAccountState(accountState: AddContactViewModel.AddressAccount) {
        addressView.loading = accountState is AddContactViewModel.AddressAccount.Loading
        addressView.error = accountState is AddContactViewModel.AddressAccount.Error
    }

    companion object {

        private const val PHOTOS_DIR = "contact_photos"
        private const val ARG_NAME = "name"
        private const val ARG_ADDRESS = "address"

        fun newInstance(wallet: WalletEntity, name: String? = null, address: String? = null): AddContactScreen {
            val fragment = AddContactScreen(wallet)
            name?.let { fragment.putStringArg(ARG_NAME, it) }
            address?.let { fragment.putStringArg(ARG_ADDRESS, it) }
            return fragment
        }
    }

}