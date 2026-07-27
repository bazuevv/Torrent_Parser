package com.tonapps.tonkeeper.ui.screen.send.contacts.add

import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.ContactsContract
import android.view.View
import android.widget.Button
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.net.toUri
import com.tonapps.tonkeeper.extensions.hideKeyboard
import com.tonapps.tonkeeper.extensions.toast
import com.tonapps.wallet.localization.Localization
import com.tonapps.tonkeeper.koin.walletViewModel
import com.tonapps.tonkeeper.ui.base.WalletContextScreen
import com.tonapps.tonkeeperx.R
import com.tonapps.blockchain.model.legacy.WalletEntity
import uikit.base.BaseFragment
import uikit.extensions.collectFlow
import uikit.widget.AsyncImageView
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

    /**
     * Системный выбор контакта: пользователь сам указывает, кого отдать приложению,
     * поэтому разрешение READ_CONTACTS не нужно — доступ выдаётся к одному URI.
     */
    private val pickContact = registerForActivityResult(ActivityResultContracts.PickContact()) { uri ->
        uri?.let { applyPickedContact(it) }
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        headerView = view.findViewById(R.id.header)
        headerView.onCloseClick = { finish() }

        nameView = view.findViewById(R.id.name)
        nameView.doOnTextChange = { viewModel.setName(it) }
        nameView.doOnIconClick = { pickContact.launch(null) }
        requireArguments().getString(ARG_NAME)?.let { nameView.text = it }

        photoView = view.findViewById(R.id.photo)

        createPhoneContactButton = view.findViewById(R.id.create_phone_contact)
        createPhoneContactButton.setOnClickListener { createPhoneContact() }

        addressView = view.findViewById(R.id.address)
        addressView.doOnTextChange = { viewModel.setAddress(it) }

        requireArguments().getString(ARG_ADDRESS)?.let { addressView.text = it }

        button = view.findViewById(R.id.button)
        button.setOnClickListener {
            hideKeyboard()
            viewModel.save()
        }

        collectFlow(viewModel.accountFlow, ::applyAccountState)
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
        contact.lookupKey?.let { viewModel.setPhoneContact(contact.name, it) }
        applyContactPhoto(contact.photoUri)
        nameView.focus()
    }

    private fun applyContactPhoto(photoUri: String?) {
        // У контакта может не быть фото — тогда прячем аватар, а не показываем пустоту
        if (photoUri.isNullOrBlank()) {
            photoView.visibility = View.GONE
            return
        }
        photoView.visibility = View.VISIBLE
        photoView.setImageURI(photoUri.toUri())
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

    private fun applyAccountState(accountState: AddContactViewModel.AddressAccount) {
        addressView.loading = accountState is AddContactViewModel.AddressAccount.Loading
        addressView.error = accountState is AddContactViewModel.AddressAccount.Error
    }

    companion object {

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