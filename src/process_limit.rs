use std::process::Child;

use goodtype_goodnotes_import::Result;

#[cfg(windows)]
pub struct ProcessLimit(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
impl ProcessLimit {
    pub fn attach(child: &Child) -> Result<Self> {
        use std::{
            mem::{size_of, zeroed},
            os::windows::io::AsRawHandle,
            ptr,
        };
        use windows_sys::Win32::System::JobObjects::*;
        // The fixed decoder is the only child. Closing the parent/job also ends decoding.
        unsafe {
            let handle = CreateJobObjectW(ptr::null(), ptr::null());
            if handle.is_null() {
                return Err(std::io::Error::last_os_error().into());
            }
            let job = Self(handle);
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
            info.BasicLimitInformation.LimitFlags =
                JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            info.ProcessMemoryLimit = 1536 * 1024 * 1024;
            if SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            ) == 0
                || AssignProcessToJobObject(handle, child.as_raw_handle()) == 0
            {
                return Err(std::io::Error::last_os_error().into());
            }
            Ok(job)
        }
    }
}

#[cfg(windows)]
impl Drop for ProcessLimit {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}

#[cfg(not(windows))]
pub struct ProcessLimit;

#[cfg(not(windows))]
impl ProcessLimit {
    pub fn attach(_child: &Child) -> Result<Self> {
        // POSIX address-space limits are set by decode.py before parsing input.
        Ok(Self)
    }
}
