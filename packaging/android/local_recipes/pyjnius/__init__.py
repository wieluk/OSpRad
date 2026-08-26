from pythonforandroid.recipe import PyProjectRecipe
from pythonforandroid.toolchain import shprint, current_directory, info
import sh
from os.path import join


class PyjniusRecipe(PyProjectRecipe):
    version = '1.7.0'
    url = 'https://github.com/kivy/pyjnius/archive/{version}.zip'
    name = 'pyjnius'
    depends = [('genericndkbuild', 'sdl2', 'sdl3'), 'six']
    site_packages_name = 'jnius'
    # OSpRad local patch (see local_recipes/numpy/__init__.py for why): pyjnius's own
    # [build-system] requires, plus the isolation flags.
    hostpython_prerequisites = ["Cython<3.2", "wheel"]
    extra_build_args = ['--no-isolation', '--skip-dependency-check']
    # OSpRad local patch: qt_jnienv_getter.patch replaces upstream's
    # genericndkbuild_jnienv_getter.patch, because pyjnius has no support for p4a's
    # "qt" bootstrap at all - the upstream patch calls WebView_AndroidGetJNIEnv(),
    # which that bootstrap never compiles in. Ours gets the JNIEnv by dlsym'ing Qt's
    # own QJniEnvironment out of the already-loaded libQt6Core; see its header.
    patches = [
        "use_cython.patch",
        "qt_jnienv_getter.patch",
    ]

    def get_recipe_env(self, arch, **kwargs):
        env = super().get_recipe_env(arch, **kwargs)

        # Taken from CythonRecipe
        env['LDFLAGS'] = env['LDFLAGS'] + ' -L{} '.format(
            self.ctx.get_libs_dir(arch.arch) +
            ' -L{} '.format(self.ctx.libs_dir) +
            ' -L{}'.format(join(self.ctx.bootstrap.build_dir, 'obj', 'local',
                                arch.arch)))
        env['LDSHARED'] = env['CC'] + ' -shared'
        env['LIBLINK'] = 'NOTNONE'

        # NDKPLATFORM is our switch for detecting Android platform, so can't be None
        env['NDKPLATFORM'] = "NOTNONE"
        return env

    def postbuild_arch(self, arch):
        super().postbuild_arch(arch)
        info('Copying pyjnius java class to classes build dir')
        with current_directory(self.get_build_dir(arch.arch)):
            shprint(sh.cp, '-a', join('jnius', 'src', 'org'), self.ctx.javaclass_dir)


recipe = PyjniusRecipe()
