import numpy as np
from numpy.matlib import repmat
import cv2
from scipy.ndimage import map_coordinates
from lib.utils import cos_window,gaussian2d_rolled_labels
from lib.fft_tools import fft2,ifft2
from .base import BaseCF
from .feature import extract_hog_feature,extract_cn_feature
from .config import ldes_config

# pad [h,w] format
def pad(img,pad):
    h,w=img.shape[:2]
    delta=(int((pad[0]-h)/2),int((pad[1]-w)/2))
    c=img.shape[2]
    r=np.zeros((pad[0],pad[1],c),dtype=np.complex64)
    idy=[delta[0],delta[0]+h]
    idx=[delta[1],delta[1]+w]
    r[idy[0]:idy[1], idx[0]:idx[1], :] = img
    return r

def parameters_to_projective_matrix(p):
    """
    :param p: [s,rot,x,y]
    :return:
    """
    s,rot,x,y=p
    R=np.array([[np.cos(rot),-np.sin(rot)],
                [np.sin(rot),np.cos(rot)]])
    T=np.diag([1.,1.,1.])
    T[:2,:2]=s*R
    T[0,2]=x
    T[1,2]=y
    return T


def getLKcorner(warp_p,sz):
    template_nx,template_ny=sz
    nx=(sz[0]-1)/2
    ny=(sz[1]-1)/2
    tmplt_pts=np.array([[-nx,-ny],
                        [-nx,template_ny-ny],
                        [template_nx-nx,template_ny-ny],
                        [template_nx-nx,-ny]]).T
    if warp_p.shape[0]==2:
        M=np.concatenate((warp_p,np.array([0,0,1])),axis=0)
        M[0,0]=M[0,0]+1
        M[1,1]=M[1,1]+1
    else:
        M=warp_p
    warp_pts=M.dot(np.concatenate((tmplt_pts,np.ones((1,4))),axis=0))
    c=np.array([[(1+template_nx)/2],[(1+template_ny)/2],[1]])
    warp_pts=warp_pts[:2,:]
    return warp_pts

def PSR(response,rate):
    max_response=np.max(response)
    h,w=response.shape
    k=4/(h*w)
    yy,xx=np.unravel_index(np.argmax(response, axis=None),response.shape)
    idx=np.arange(w)-xx
    idy=np.arange(h)-yy
    idx=repmat(idx,h,1)-xx
    idy=repmat(idy,w,1).T-yy
    t=idx**2+idy**2
    delta=1-np.exp(-k*t)
    r=(max_response-response)/delta
    return np.min(r)


def get_center_likelihood(likelihood_map, sz):
    h,w=likelihood_map.shape[:2]
    n1= h - sz[1] + 1
    n2= w - sz[0] + 1
    sat=cv2.integral(likelihood_map)
    i,j=np.arange(n1),np.arange(n2)
    i,j=np.meshgrid(i,j)
    sat1=sat[i,j]
    sat2=np.roll(sat, -sz[1], axis=0)
    sat2=np.roll(sat2, -sz[0], axis=1)
    sat2=sat2[i,j]
    sat3=np.roll(sat, -sz[1], axis=0)
    sat3=sat3[i,j]
    sat4=np.roll(sat, -sz[0], axis=1)
    sat4=sat4[i,j]
    center_likelihood=(sat1+sat2-sat3-sat4)/(sz[0] * sz[1])
    def fillzeros(im,sz):
        res=np.zeros((sz[1],sz[0]))
        msz=((sz[0]-im.shape[1])//2,(sz[1]-im.shape[0])//2)
        res[msz[1]:msz[1]+im.shape[0],msz[0]:msz[0]+im.shape[1]]=im
        return res
    center_likelihood=fillzeros(center_likelihood,(h,w))
    return center_likelihood.T

class LDES(BaseCF):
    def __init__(self,config):
        super(LDES).__init__()
        self.kernel_type=config.kernel_type
        self.padding=config.padding
        self.lambda_ = config.lambda_
        self.output_sigma_factor = config.output_sigma_factor
        self.interp_factor = config.interp_factor
        self.cell_size = config.cell_size

        self.min_image_sample_size =config.min_image_sample_size
        self.max_image_sample_size = config.max_image_sample_size

        self.fixed_model_sz = config.fixed_model_sz
        self.is_rotation = config.is_rotation
        self.is_BGD = self.is_rotation
        self.is_subpixel = config.is_subpixel
        self.interp_n = config.interp_n

        self.learning_rate_scale = config.learning_rate_scale
        self.scale_sz_window = config.scale_sz_window

        # color histogram
        self.inter_patch_rate = config.inter_patch_rate
        self.nbin = config.nbin
        self.color_update_rate = config.color_update_rate
        self.merge_factor = config.merge_factor

        self.polygon=config.polygon
        self.vis=False
        self.use_color_hist=config.use_color_hist
        self.sigma=config.sigma

    def init(self, first_frame, region):
        assert len(first_frame.shape)==3 and first_frame.shape[2]==3
        region = np.array(region).astype(np.int64)
        if self.polygon is False:
            x0, y0, w, h = tuple(region)
            rot=0
            self._center = (x0 + w / 2, y0 + h / 2)
            self.target_sz = (w, h)
        else:
            corners=region.reshape((4,2))
            lu=np.min(corners,axis=0)
            pos=np.mean(corners,axis=0)
            pos=pos.T
            lu=lu.T
            target_sz=(pos-lu)*2
            self.target_sz=(target_sz[0],target_sz[1])
            self._center=(pos[0],pos[1])
            A=np.array([0.,-1.])
            B=np.array([region[4]-region[2],region[3]-region[5]])
            rot1=np.arccos(A.dot(B)/(np.linalg.norm(A)*np.linalg.norm(B)))*2/np.pi
            if np.prod(B)<0:
                rot1=-rot1
            C=np.array([region[6]-region[2],region[1]-region[7]])
            rot2=np.arccos(A.dot(C)/(np.linalg.norm(A)*np.linalg.norm(C)))*2/np.pi
            if np.prod(C)<0:
                rot2=-rot2
            rot=(rot1+rot2)/2



        self.bin_mapping=self.get_bin_mapping(self.nbin)

        self.window_sz=(int(np.floor(self.target_sz[0] * (1 + self.padding))), int(np.floor(self.target_sz[1] * (1 + self.padding))))
        search_area= self.window_sz[0] * self.window_sz[1]
        self.sc=search_area/np.clip(search_area,a_min=self.min_image_sample_size,a_max=self.max_image_sample_size)
        self.window_sz0=(int(np.round(self.window_sz[0] / self.sc)), int(np.round(self.window_sz[1] / self.sc)))
        feature_sz=(self.window_sz0[0] // self.cell_size, self.window_sz0[1] // self.cell_size)
        self.window_sz0=(feature_sz[0] * self.cell_size, feature_sz[1] * self.cell_size)

        self.sc= self.window_sz[0] / self.window_sz0[0]
        self.cell_size=int(np.round((self.window_sz0[0] / feature_sz[0])))
        self.rot=rot
        self.avg_dim= (self.window_sz[0] + self.window_sz[1]) / 4
        self.window_sz_search=(int(np.floor(self.window_sz[0]+self.avg_dim)),int(np.floor(self.window_sz[1]+self.avg_dim)))
        self.window_sz_search0=(int(np.floor(self.window_sz_search[0]/self.sc)),int(np.floor(self.window_sz_search[1]/self.sc)))
        cell_size_search=self.cell_size
        feature_sz0=(int(np.floor(self.window_sz_search0[0]/cell_size_search)),
                     int(np.floor(self.window_sz_search0[1]/cell_size_search)))
        residual=(feature_sz0[0]-feature_sz[0],feature_sz0[1]-feature_sz[1])
        feature_sz0=(feature_sz0[0]+residual[0]%2,feature_sz0[1]+residual[1]%2)
        self.window_sz_search0=(feature_sz0[0]*cell_size_search,
                                feature_sz0[1]*cell_size_search)
        self.sc=self.window_sz_search[0]/self.window_sz_search0[0]
        self.target_sz0=(int(np.round(self.target_sz[0]/self.sc)),
                         int(np.round(self.target_sz[1]/self.sc)))
        self.output_sigma=np.sqrt(self.target_sz[0]*self.target_sz[1])*self.output_sigma_factor/self.cell_size
        self.y=gaussian2d_rolled_labels((int(np.floor(self.window_sz0[0]/self.cell_size)),
                                         int(np.floor(self.window_sz0[1]/self.cell_size))),
                                        self.output_sigma)
        self.yf=fft2(self.y)
        self.cos_window=cos_window((self.y.shape[1],self.y.shape[0]))
        self.cos_window_search=cos_window((int(np.floor(self.window_sz_search0[0]/cell_size_search)),
                                           int(np.floor(self.window_sz_search0[1]/cell_size_search))))
        # scale setttings
        avg_dim=(self.target_sz[0]+self.target_sz[1])/2.5
        self.scale_sz=((self.target_sz[0]+avg_dim)/self.sc,
                       (self.target_sz[1]+avg_dim)/self.sc)
        self.scale_sz0=self.scale_sz
        self.cos_window_scale=cos_window((self.scale_sz_window[0],self.scale_sz_window[1]))
        self.mag=self.cos_window_scale.shape[0]/np.log(np.sqrt((self.cos_window_scale.shape[0]**2+
                                                               self.cos_window_scale.shape[1]**2)/4))

        self.cell_size=cell_size_search
        tmp_sc = 1.
        tmp_rot = 0.
        self.logupdate(1,first_frame,self._center,tmp_sc,tmp_rot)
        x,y=self._center
        x=np.clip(x,a_min=0,a_max=first_frame.shape[1]-1)
        y=np.clip(y,a_min=0,a_max=first_frame.shape[0]-1)
        self._center=(x,y)
        # construct return variables

    def update(self,current_frame,vis=False):
        self.vis=vis
        pos,tmp_sc,tmp_rot,cscore,sscore=self.tracking(current_frame,self._center,0)
        if self.is_BGD:
            cscore=(1-self.interp_n)*cscore+self.interp_n*sscore
            iter=0
            mcscore=0
            mpos=None
            msc = None
            mrot = None
            while iter<5:
                if np.floor(self.sc*tmp_sc*self.window_sz0[0])+np.floor(self.sc*tmp_sc*self.window_sz0[1])<10:
                    tmp_sc=1.
                self.sc=self.sc*tmp_sc
                self.rot=self.rot+tmp_rot
                if cscore>=mcscore:
                    msc=self.sc
                    mrot=self.rot
                    mpos=pos
                    mcscore=cscore
                else:
                    break
                pos,tmp_sc,tmp_rot,cscore,sscore=self.tracking(current_frame,pos,iter)
                cscore=(1-self.interp_n)*cscore+self.interp_n*sscore
                iter+=1
            if msc is not None:
                pos = mpos
                self.sc = msc
                self.rot = mrot

        self.logupdate(0,current_frame,pos,tmp_sc,tmp_rot)
        x, y = pos
        x = np.clip(x, a_min=0, a_max=current_frame.shape[1]-1)
        y = np.clip(y, a_min=0, a_max=current_frame.shape[0]-1)
        self._center=(x,y)
        target_sz=(self.sc*self.target_sz0[0],self.sc*self.target_sz0[1])
        box=[x-target_sz[0]/2,y-target_sz[1]/2,target_sz[0],target_sz[1]]
        aff=[]
        if self.is_rotation:
            T=parameters_to_projective_matrix([1,self.rot,self._center[0],self._center[1]])
            aff=getLKcorner(T,target_sz)
            """
            import copy
            show_img=copy.deepcopy(current_frame)
            tl=(int(aff[0,0]),int(aff[1,0]))
            tr=(int(aff[0,1]),int(aff[1,1]))
            br=(int(aff[0,2]),int(aff[1,2]))
            bl=(int(aff[0,3]),int(aff[1,3]))
            show_img=cv2.line(show_img,tl,tr,color=(255,0,0))
            show_img=cv2.line(show_img,tr,br,color=(255,0,0))
            show_img=cv2.line(show_img,br,bl,color=(255,0,0))
            show_img=cv2.line(show_img,bl,tl,color=(255,0,0))
            cv2.imshow('show_img',show_img)
            cv2.waitKey(1)
            """

        self.aff=aff
        if self.polygon is True:
            aff=aff[:,[0,3,2,1]]
            reg=aff.T.flatten()
            return reg
        else:
            return box

    def logupdate(self,init,img,pos,tmp_sc,tmp_rot):
        tmp=np.floor(self.sc*tmp_sc*self.window_sz0[0])+np.floor(self.sc*tmp_sc*self.window_sz0[1])
        if tmp<10:
            tmp_sc=1.
        self.sc=self.sc*tmp_sc
        self.rot=self.rot+tmp_rot
        self.window_sz=(int(np.floor(self.sc*self.window_sz0[0])),
                        int(np.floor(self.sc*self.window_sz0[1])))
        self.window_sz_search=(int(np.floor(self.sc*self.window_sz_search0[0])),
                               int(np.floor(self.sc*self.window_sz_search0[1])))
        # compute the current CF model
        # sampling the image
        if self.is_rotation:
            patch=self.get_affine_subwindow(img, pos, self.sc, self.rot, self.window_sz0)
        else:
            patchO=cv2.getRectSubPix(img,self.window_sz,pos)
            patch=cv2.resize(patchO,self.window_sz0)
        x=self.get_features(patch,self.cell_size)
        x=x*self.cos_window[:,:,None]
        xf=fft2(x)
        #kf=np.sum(xf*np.conj(xf),axis=2)/xf.size
        kf=self._kernel_correlation(xf,xf,self.kernel_type)
        alphaf=self.yf/(kf+self.lambda_)

        if self.is_rotation:
            # here is not similarity transformation
            patchL=self.get_affine_subwindow(img, pos,1., self.rot, (int(np.floor(self.sc* self.scale_sz[0])),
                                                                          int(np.floor(self.sc*self.scale_sz[1]))))
        else:
            patchL=cv2.getRectSubPix(img,(int(np.floor(self.sc*self.scale_sz[0])),
                                               int(np.floor(self.sc*self.scale_sz[1]))),pos)
        patchL=cv2.resize(patchL,self.scale_sz_window)
        # get logpolar space and apply feature extraction
        patchLp=cv2.logPolar(patchL.astype(np.float32),((patchL.shape[1]-1)/2,(patchL.shape[0]-1)/2),self.mag,flags=cv2.INTER_LINEAR + cv2.WARP_FILL_OUTLIERS)

        patchLp=extract_hog_feature(patchLp,self.cell_size)
        #patchLp = patchLp * self.cos_window_scale[:, :, None]

        # updating color histogram probabilities
        sz=(patch.shape[1],patch.shape[0])

        #is_color=True
        if self.use_color_hist:
            pos_in=((sz[0]-1)/2,(sz[1]-1)/2)
            lab_patch=patch
            inter_patch=cv2.getRectSubPix(lab_patch.astype(np.uint8),(int(round(sz[0]*self.inter_patch_rate)),int(round(sz[1]*self.inter_patch_rate))),pos_in)
            self.interp_patch=inter_patch
            pl=self.get_color_space_hist(lab_patch,self.nbin)
            pi=self.get_color_space_hist(inter_patch,self.nbin)
        interp_factor_scale=self.learning_rate_scale
        if init==1: # first_frame
            self.model_alphaf=alphaf
            self.model_xf=xf
            self.model_patchLp=patchLp
            if self.use_color_hist:
                self.pl=pl
                self.pi=pi

        else:
            # CF model
            self.model_alphaf=(1-self.interp_factor)*self.model_alphaf+self.interp_factor*alphaf
            self.model_xf=(1-self.interp_factor)*self.model_xf+self.interp_factor*xf
            self.model_patchLp= (1 - interp_factor_scale) * self.model_patchLp + interp_factor_scale * patchLp
            if self.use_color_hist:
                self.pi=(1-self.color_update_rate)*self.pi+self.color_update_rate*pi
                self.pl=(1-self.color_update_rate)*self.pl+self.color_update_rate*pl


    def tracking(self,img,pos,polish):
        """
        obtain a subwindow for detecting at the positiono from last frame, and convert to Fourier domain
        find  a proper window size
        :param img:
        :param pos:
        :param iter:
        :return:
        """

        large_num=0
        if polish>large_num:
            w_sz0=self.window_sz0
            c_w=self.cos_window
        else:
            w_sz0=self.window_sz_search0
            c_w=self.cos_window_search
        if self.is_rotation:
            patch=self.get_affine_subwindow(img, pos, self.sc, self.rot, w_sz0)
        else:
            sz_s=(int(np.floor(self.sc*w_sz0[0])),int(np.floor(self.sc*w_sz0[1])))
            patchO=cv2.getRectSubPix(img,sz_s,pos)
            patch=cv2.resize(patchO,w_sz0)

        z=self.get_features(patch,self.cell_size)
        z=z*c_w[:,:,None]
        zf=fft2(z)
        ssz=(zf.shape[1],zf.shape[0],zf.shape[2])

        # calculate response of the classifier at all shifts
        wf=np.conj(self.model_xf)*self.model_alphaf[:,:,None]/np.size(self.model_xf)
        if polish<=large_num:
            w=pad(ifft2(wf),(ssz[1],ssz[0]))
            wf=fft2(w)

        tmp_sz=ssz
        # compute convolution for each feature block in the Fourier domain
        # use general compute here for easy extension in future
        #print('wf.shape:',wf.shape)
        #print('zf.shape:',zf.shape)
        rff=np.sum(wf*zf,axis=2)
        response_cf=np.real(ifft2(rff))
        response_cf=np.fft.fftshift(response_cf,axes=(0,1))

        response_cf=cv2.resize(response_cf,(tmp_sz[0],tmp_sz[1]))
        response_color=np.zeros_like(response_cf)

        if self.use_color_hist:
            object_likelihood=self.get_colour_map(patch,self.pl,self.pi,self.bin_mapping)
            response_color=get_center_likelihood(object_likelihood,self.target_sz0)
            response_color=cv2.resize(response_color,(response_cf.shape[1],response_cf.shape[0]))



        response=(1-self.merge_factor)*response_cf+self.merge_factor*response_color
        if self.vis is True:
            self.score=response
            self.crop_size=self.window_sz
        # sub-pixel search
        pty,ptx=np.unravel_index(np.argmax(response, axis=None),response.shape)
        if self.is_subpixel:
            slobe=2
            idy=np.arange(pty-slobe,pty+slobe+1)
            idx=np.arange(ptx-slobe,ptx+slobe+1)
            idy=np.clip(idy,a_min=0,a_max=response.shape[0]-1)
            idx=np.clip(idx,a_min=0,a_max=response.shape[1]-1)
            weight_patch=response[idy,:][:,idx]
            s=np.sum(weight_patch)+2e-16
            pty=np.sum(np.sum(weight_patch,axis=1)*idy)/s
            ptx=np.sum(np.sum(weight_patch,axis=0)*idx)/s
        cscore=PSR(response,0.1)

        # update the translation status
        dy=pty-((response.shape[0]-1)/2)
        dx=ptx-((response.shape[1]-1)/2)

        if self.is_rotation:
            sn,cs=np.sin(self.rot),np.cos(self.rot)
            pp=np.array([[self.sc*cs,-self.sc*sn],
                         [self.sc*sn,self.sc*cs]])
            x,y=pos
            delta=self.cell_size*np.array([[dy,dx]]).dot(pp)
            x+=delta[0,1]
            y+=delta[0,0]
            pos=(x,y)
            patchL=self.get_affine_subwindow(img, pos, 1., self.rot, (int(np.floor(self.sc* self.scale_sz[0])),
                                                                          int(np.floor(self.sc*self.scale_sz[1]))))

        else:
            x,y=pos
            pos=(x+self.sc*self.cell_size*dx,y+self.sc*self.cell_size*dy)
            patchL=cv2.getRectSubPix(img,(int(np.floor(self.sc*self.scale_sz[0])),
                                               int(np.floor(self.sc*self.scale_sz[1]))),pos)
        patchL=cv2.resize(patchL,self.scale_sz_window)
        # convert into logpolar
        patchLp=cv2.logPolar(patchL.astype(np.float32),((patchL.shape[1]-1)/2,(patchL.shape[0]-1)/2),self.mag,flags=cv2.INTER_LINEAR + cv2.WARP_FILL_OUTLIERS)
        #cv2.imshow('polar',patchLp.astype(np.uint8))
        #cv2.waitKey(1)

        patchLp=extract_hog_feature(patchLp,self.cell_size)
        #patchLp = patchLp * self.cos_window_scale[:, :, None]

        tmp_sc,tmp_rot,sscore=self.estimate_scale(self.model_patchLp, patchLp, self.mag)
        tmp_sc=np.clip(tmp_sc,a_min=0.6,a_max=1.4)
        if tmp_rot>1 or tmp_rot<-1:
            tmp_rot=0

        return pos, tmp_sc, tmp_rot, cscore, sscore

    def estimate_scale(self,model,obser,mag):
        def phase_correlation(src1,src2):
            s1f=fft2(src1)
            s2f=fft2(src2)
            num=s2f*np.conj(s1f)
            d=np.sqrt(num*np.conj(num))+2e-16
            Cf=np.sum(num/d,axis=2)
            C=np.real(ifft2(Cf))
            C=np.fft.fftshift(C,axes=(0,1))

            mscore=np.max(C)
            pty,ptx=np.unravel_index(np.argmax(C, axis=None), C.shape)
            slobe_y=slobe_x=1
            idy=np.arange(pty-slobe_y,pty+slobe_y+1).astype(np.int64)
            idx=np.arange(ptx-slobe_x,ptx+slobe_x+1).astype(np.int64)
            idy=np.clip(idy,a_min=0,a_max=C.shape[0]-1)
            idx=np.clip(idx,a_min=0,a_max=C.shape[1]-1)
            weight_patch=C[idy,:][:,idx]

            s=np.sum(weight_patch)+2e-16
            pty=np.sum(np.sum(weight_patch,axis=1)*idy)/s
            ptx=np.sum(np.sum(weight_patch,axis=0)*idx)/s
            pty=pty-(src1.shape[0])//2
            ptx=ptx-(src1.shape[1])//2
            return ptx,pty,mscore

        ptx,pty,mscore=phase_correlation(model,obser)
        rotate=pty*np.pi/((obser.shape[1])//2)
        scale = np.exp(ptx / mag)
        #print('scale:',scale)
        #print('rotate:',rotate)
        return scale,rotate,mscore

    def get_features(self,img,cell_size):
        hog_feature=extract_hog_feature(img.astype(np.uint8),cell_size)
        cn_feature=extract_cn_feature(img.astype(np.uint8),cell_size)
        return np.concatenate((hog_feature,cn_feature),axis=2)

    def get_color_space_hist(self,patch,n_bins):
        histogram=cv2.calcHist([patch.astype(np.uint8)],[0,1,2],None,[n_bins,n_bins,n_bins],[0,256,0,256,0,256])/patch.size
        return histogram

    def get_colour_map(self,patch,bg_hist,fg_hist,bin_mapping):
        frame_bin = cv2.LUT(patch.astype(np.uint8), bin_mapping).astype(np.int64)
        P_fg = fg_hist[frame_bin[:, :, 0], frame_bin[:, :, 1], frame_bin[:, :, 2]]
        P_bg=bg_hist[frame_bin[:,:,0],frame_bin[:,:,1],frame_bin[:,:,2]]
        #not_na=np.where(P_bg!=0)
        #P_O=0.*np.ones_like(P_fg)
        #P_O[not_na]=P_fg[not_na]/P_bg[not_na]
        P_O=P_fg/(P_fg+P_bg)
        P_O[np.isnan(P_O)]=0.5
        return P_O

    def get_bin_mapping(self,num_bins):
        bin_mapping = np.zeros((256,))
        for i in range(bin_mapping.shape[0]):
            bin_mapping[i] = (np.floor(i / (256 / num_bins)))
        return bin_mapping.astype(np.int)


    def get_affine_subwindow(self,img, pos, sc, rot, window_sz):
        def simiparam2mat(tx,ty,rot,s):
            sn,cs=s*np.sin(rot),s*np.cos(rot)
            p=[tx,ty,cs,-sn,sn,cs]
            return p

        def interp2(img, Xq, Yq):
            wimg = map_coordinates(img, [Xq.ravel(), Yq.ravel()], order=1, mode='constant')
            wimg = wimg.reshape(Xq.shape)
            return wimg

        def mwarpimg(img,p,sz):
            imsz=img.shape
            w,h=sz
            x,y=np.meshgrid(np.arange(w).astype(np.int64)-w//2,np.arange(h).astype(np.int64)-h//2)
            tmp1=np.zeros((h*w,3))
            tmp1[:,0]=1
            tmp1[:,1]=x.flatten()
            tmp1[:,2]=y.flatten()
            tmp2=np.array([[p[0],p[1]],[p[2],p[4]],[p[3],p[5]]])
            tmp3=tmp1.dot(tmp2)
            tmp3=np.clip(tmp3,a_min=0,a_max=None)
            tmp3[:, 0] = np.clip(tmp3[:, 0], a_min=None,a_max=imsz[1]-1)
            tmp3[:, 1] = np.clip(tmp3[:, 1], a_min=None,a_max=imsz[0]-1)
            pos=np.reshape(tmp3,(h,w,2))
            c=img.shape[2]
            wimg=np.zeros((sz[1],sz[0],c))
            for i in range(c):
                wimg[:,:,i]=interp2(img[:,:,i],pos[:,:,1],pos[:,:,0])
            return wimg
        x,y=pos
        param0=simiparam2mat(x,y,rot,sc)
        out=mwarpimg(img.astype(np.float32),param0,window_sz)
        #cv2.imshow('affine_window',out.astype(np.uint8))
        #cv2.waitKey(1)
        return out

    def _kernel_correlation(self, xf, yf, kernel='gaussian'):
        if kernel== 'gaussian':
            N=xf.shape[0]*xf.shape[1]
            xx=(np.dot(xf.flatten().conj().T,xf.flatten())/N)
            yy=(np.dot(yf.flatten().conj().T,yf.flatten())/N)
            xyf=xf*np.conj(yf)
            xy=np.sum(np.real(ifft2(xyf)),axis=2)
            kf = fft2(np.exp(-1 / self.sigma ** 2 * np.clip(xx+yy-2*xy,a_min=0,a_max=None) / np.size(xf)))
        elif kernel== 'linear':
            kf= np.sum(xf*np.conj(yf),axis=2)/np.size(xf)
        else:
            raise NotImplementedError
        return kf

    """
        def get_affine_subwindow(self,img, pos, sc, rot, window_sz):
        def pad_img(img,border):
            x1,y1,x2,y2=border
            h,w=img.shape[:2]
            left=int(max(0,-x1))
            right=int(max(0,x2-(w-1)))
            top=int(max(0,-y1))
            bottom=int(max(0,y2-(h-1)))
            return cv2.copyMakeBorder(img,top,bottom,left,right,cv2.BORDER_REFLECT)

        rot_M=cv2.getRotationMatrix2D(pos,-rot,sc[0])
        rot_M=cv2.transpose(rot_M)
        x,y=pos
        w,h=window_sz
        corners=np.array([
            [x-w/2,y-h/2,1.],
            [x-w/2,y+h/2,1.],
            [x+w/2,y+h/2,1.],
            [x+w/2,y-h/2,1.]
        ])
        wcorners=corners.dot(rot_M)
        x_min,x_max=np.min(wcorners[:,0]),np.max(wcorners[:,0])
        y_min,y_max=np.min(wcorners[:,1]),np.max(wcorners[:,1])
        padded=pad_img(img,(x_min,y_min,x_max,y_max))
        p=(x+x_min,y+y_min)

        rot_M=cv2.getRotationMatrix2D(p,-rot,1./sc[0])
        M1=np.zeros((3,3))
        M1[2,2]=1.
        M1[:2,:3]=rot_M
        shift=np.array([[1.,0,(w-1)/2-x],
                        [0,1,(h-1)/2-y]])
        M=shift.dot(M1)
        patch=cv2.warpAffine(padded,M,window_sz)
        return patch
    """





